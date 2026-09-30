#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { appendFile, mkdir, open, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const dataRoot = resolve(
	process.env.RIBAO_WEREAD_OMNI_DATA_ROOT ||
		join(homedir(), "Library", "Application Support", "beijing-daily-brief", "weread-omni-test"),
);
const packageRoot = resolve(process.env.WEREAD_OMNI_PACKAGE_ROOT || join(dataRoot, "source"));
const configDir = resolve(process.env.WEREAD_CONFIG_DIR || join(dataRoot, "config"));
const libraryDir = resolve(process.env.WEREAD_LIBRARY_DIR || join(dataRoot, "library"));
const probeRoot = join(dataRoot, "overnight-probes");
const pointerPath = join(probeRoot, "current.json");
const lockPath = join(probeRoot, "probe.lock");
const accountAlias = process.env.WEREAD_ACCOUNT || "default";
const backoffHours = [1, 2, 6, 12, 24];

function boundedInteger(value, fallback, minimum, maximum) {
	const parsed = Number(value);
	if (!Number.isSafeInteger(parsed)) return fallback;
	return Math.min(maximum, Math.max(minimum, parsed));
}

function iso(value = new Date()) {
	return value.toISOString();
}

function compact(value = new Date()) {
	return iso(value).replaceAll(/[-:.TZ]/g, "").slice(0, 14);
}

function hashId(value) {
	return createHash("sha256").update(String(value)).digest("hex").slice(0, 12);
}

function nextMorning0830(now) {
	const parts = Object.fromEntries(
		new Intl.DateTimeFormat("en-CA", {
			timeZone: "Asia/Shanghai",
			year: "numeric",
			month: "2-digit",
			day: "2-digit",
		}).formatToParts(now).map((part) => [part.type, part.value]),
	);
	let stop = new Date(`${parts.year}-${parts.month}-${parts.day}T08:30:00+08:00`);
	if (stop <= now) stop = new Date(stop.getTime() + 24 * 60 * 60 * 1000);
	return stop;
}

async function readJson(path, fallback) {
	try {
		return JSON.parse(await readFile(path, "utf8"));
	} catch (error) {
		if (error?.code === "ENOENT") return fallback;
		throw error;
	}
}

async function atomicWrite(path, value) {
	await mkdir(dirname(path), { recursive: true, mode: 0o700 });
	const temporary = join(dirname(path), `.${randomUUID()}.tmp`);
	await writeFile(temporary, typeof value === "string" ? value : `${JSON.stringify(value, null, 2)}\n`, {
		mode: 0o600,
	});
	await rename(temporary, path);
}

async function appendEvent(path, event) {
	await mkdir(dirname(path), { recursive: true, mode: 0o700 });
	await appendFile(path, `${JSON.stringify(event)}\n`, { encoding: "utf8", mode: 0o600 });
}

async function acquireLock() {
	await mkdir(dirname(lockPath), { recursive: true, mode: 0o700 });
	for (let attempt = 0; attempt < 2; attempt += 1) {
		try {
			const handle = await open(lockPath, "wx", 0o600);
			await handle.writeFile(`${JSON.stringify({ pid: process.pid, startedAt: iso() })}\n`);
			return handle;
		} catch (error) {
			if (error?.code !== "EEXIST") throw error;
			const existing = await readJson(lockPath, {});
			const pid = Number(existing.pid);
			let active = Number.isSafeInteger(pid) && pid > 0;
			if (active) {
				try {
					process.kill(pid, 0);
				} catch (probeError) {
					active = probeError?.code === "EPERM";
				}
			}
			if (active || attempt > 0) return undefined;
			await rm(lockPath, { force: true });
		}
	}
	return undefined;
}

function classify(error) {
	const code = Number(error?.errCode);
	const status = Number(error?.status);
	if (code === -2041) return "human_verification";
	if (code === -2014) return "rate_limited";
	if (code === -2012 || status === 401 || error?.name === "AuthError") return "authentication";
	if (["TransportError", "TimeoutError", "AbortError"].includes(error?.name) || status >= 500 || status === 0) return "transport";
	return "source_error";
}

function parseEvents(text) {
	return text.split(/\r?\n/).filter(Boolean).flatMap((line) => {
		try {
			return [JSON.parse(line)];
		} catch {
			return [];
		}
	});
}

function summarize(plan, state, events) {
	const ticks = events.filter((event) => event.kind === "tick").map((event) => new Date(event.at).getTime());
	let sleepGaps = 0;
	for (let index = 1; index < ticks.length; index += 1) {
		if (ticks[index] - ticks[index - 1] > 25 * 60 * 1000) sleepGaps += 1;
	}
	const requests = events
		.filter((event) => Number(event.remoteRequests) > 0)
		.map((event) => new Date(event.at).getTime())
		.sort((left, right) => left - right);
	let maxRequestsPerHour = 0;
	for (let left = 0, right = 0; right < requests.length; right += 1) {
		while (requests[right] - requests[left] >= 60 * 60 * 1000) left += 1;
		maxRequestsPerHour = Math.max(maxRequestsPerHour, right - left + 1);
	}
	const successfulHashes = new Set(
		events.filter((event) => event.kind === "articles_success").map((event) => event.accountHash),
	);
	const count = (kind) => events.filter((event) => event.kind === kind).length;
	const accountCount = Number(state.accountCount || state.accountOrder?.length || 0);
	const humanVerificationCount = count("human_verification");
	const authenticationFailureCount = count("authentication_failure");
	let verdict = "PARTIAL";
	if (humanVerificationCount > 0 || authenticationFailureCount > 0) verdict = "FAIL";
	else if (accountCount > 0 && successfulHashes.size >= accountCount && sleepGaps === 0) verdict = "PASS";
	return {
		verdict,
		run_id: plan.runId,
		started_at: plan.startAt,
		planned_stop_at: plan.stopAt,
		mode: "article_list_only",
		article_request_count: plan.articleRequestCount,
		request_timeout_ms: plan.requestTimeoutMs,
		account_count: accountCount,
		unique_accounts_succeeded: successfulHashes.size,
		article_list_successes: count("articles_success"),
		human_verification_count: humanVerificationCount,
		authentication_failure_count: authenticationFailureCount,
		rate_limit_count: count("rate_limited"),
		transport_failure_count: count("transport_failure"),
		other_source_failure_count: count("source_failure"),
		cooldown_skip_count: count("cooldown_skip"),
		sleep_gap_count: sleepGaps,
		max_observed_api_operations_per_hour: maxRequestsPerHour,
		baseline_human_verification: Boolean(plan.baselineHumanVerification),
		full_text_tested: false,
	};
}

function markdown(summary) {
	return `# weread-omni 夜间低频验收\n\n` +
		`- 结论：**${summary.verdict}**\n` +
		`- 模式：仅文章列表（未抓取全文）\n` +
		`- 每次文章数：${summary.article_request_count}\n` +
		`- 单次请求超时：${summary.request_timeout_ms}ms\n` +
		`- 计划时段：${summary.started_at} — ${summary.planned_stop_at}\n` +
		`- 订阅源数：${summary.account_count}\n` +
		`- 已成功覆盖：${summary.unique_accounts_succeeded}\n` +
		`- 文章列表成功请求：${summary.article_list_successes}\n` +
		`- -2041 人机验证：${summary.human_verification_count}\n` +
		`- 登录失效：${summary.authentication_failure_count}\n` +
		`- 限流：${summary.rate_limit_count}\n` +
		`- 网络/上游失败：${summary.transport_failure_count}\n` +
		`- 睡眠或调度断档：${summary.sleep_gap_count}\n` +
		`- 观测到的最高每小时 API 操作数：${summary.max_observed_api_operations_per_hour}\n\n` +
		`> PASS 只代表公众号文章列表低频访问通过；全文获取需单独验收。\n`;
}

async function loadRun() {
	const pointer = await readJson(pointerPath, undefined);
	if (!pointer?.runDir) throw new Error("overnight probe is not initialized");
	const runDir = resolve(pointer.runDir);
	return {
		runDir,
		planPath: join(runDir, "plan.json"),
		statePath: join(runDir, "state.json"),
		ledgerPath: join(runDir, "events.jsonl"),
		reportPath: join(runDir, "report.md"),
	};
}

async function initialize() {
	const now = new Date();
	const stop = nextMorning0830(now);
	const runId = `overnight-${compact(now)}`;
	const runDir = join(probeRoot, runId);
	const articleRequestCount = boundedInteger(process.env.RIBAO_OVERNIGHT_ARTICLE_COUNT, 2, 1, 10);
	const requestTimeoutMs = boundedInteger(process.env.RIBAO_OVERNIGHT_REQUEST_TIMEOUT_MS, 15000, 5000, 60000);
	const initialCooldownMinutes = boundedInteger(process.env.RIBAO_OVERNIGHT_INITIAL_COOLDOWN_MINUTES, 0, 0, 1440);
	const initialBlockedUntil = new Date(now.getTime() + initialCooldownMinutes * 60 * 1000);
	const plan = {
		version: 1,
		runId,
		startAt: iso(now),
		stopAt: iso(stop),
		initialBlockedUntil: iso(initialBlockedUntil),
		intervalMinutes: 15,
		mode: "article_list_only",
		articleRequestCount,
		requestTimeoutMs,
		initialCooldownMinutes,
		baselineHumanVerification: false,
	};
	const state = {
		version: 1,
		cursor: 0,
		accountCount: 0,
		accountOrder: [],
		accounts: {},
		blockedUntil: plan.initialBlockedUntil,
		backoffIndex: 0,
		consecutiveSuccesses: 0,
		loginRequired: false,
		completedAt: null,
	};
	await mkdir(runDir, { recursive: true, mode: 0o700 });
	await atomicWrite(join(runDir, "plan.json"), plan);
	await atomicWrite(join(runDir, "state.json"), state);
	await appendEvent(join(runDir, "events.jsonl"), {
		at: iso(now), kind: "initialized", baseline: "low_frequency_fresh_probe",
		articleRequestCount, requestTimeoutMs, initialCooldownMinutes,
	});
	await atomicWrite(pointerPath, { version: 1, runId, runDir });
	process.stdout.write(`${JSON.stringify({ status: "initialized", run_id: runId, stop_at: plan.stopAt, report: join(runDir, "report.md") })}\n`);
}

async function notifyAndUnload(summary, reportPath) {
	const message = `结果 ${summary.verdict}；覆盖 ${summary.unique_accounts_succeeded}/${summary.account_count}；-2041 ${summary.human_verification_count} 次。`;
	spawnSync("/usr/bin/osascript", ["-e", `display notification ${JSON.stringify(message)} with title "公众号夜间测试完成"`], { timeout: 10000 });
	// Stop the auxiliary awake assertion before asking launchd to unload this
	// currently running probe job; self-unloading first could prevent cleanup.
	for (const path of [process.env.RIBAO_OVERNIGHT_AWAKE_PLIST, process.env.RIBAO_OVERNIGHT_PROBE_PLIST]) {
		if (!path) continue;
		spawnSync("/bin/launchctl", ["bootout", `gui/${process.getuid()}`, path], { timeout: 10000 });
	}
	return reportPath;
}

async function finish(run, plan, state, events, markComplete = true) {
	const summary = summarize(plan, state, events);
	await atomicWrite(run.reportPath, markdown(summary));
	await atomicWrite(join(run.runDir, "summary.json"), summary);
	if (markComplete && !state.completedAt) {
		state.completedAt = iso();
		await atomicWrite(run.statePath, state);
		await appendEvent(run.ledgerPath, { at: iso(), kind: "completed", verdict: summary.verdict });
		await notifyAndUnload(summary, run.reportPath);
	}
	process.stdout.write(`${JSON.stringify({ status: markComplete ? "completed" : "status", ...summary, report: run.reportPath })}\n`);
}

async function step() {
	const lock = await acquireLock();
	if (!lock) {
		process.stdout.write(`${JSON.stringify({ status: "already_running" })}\n`);
		return;
	}
	try {
		const run = await loadRun();
		const plan = await readJson(run.planPath, {});
		const state = await readJson(run.statePath, {});
		const now = new Date();
		await appendEvent(run.ledgerPath, { at: iso(now), kind: "tick" });
		if (state.completedAt) {
			process.stdout.write(`${JSON.stringify({ status: "already_completed" })}\n`);
			return;
		}
		if (now >= new Date(plan.stopAt)) {
			const events = parseEvents(await readFile(run.ledgerPath, "utf8"));
			await finish(run, plan, state, events, true);
			return;
		}
		if (now < new Date(state.blockedUntil || 0)) {
			await appendEvent(run.ledgerPath, { at: iso(now), kind: "cooldown_skip" });
			process.stdout.write(`${JSON.stringify({ status: "cooldown", blocked_until: state.blockedUntil })}\n`);
			return;
		}
		process.env.WEREAD_CONFIG_DIR = configDir;
		process.env.WEREAD_LIBRARY_DIR = libraryDir;
		process.env.WEREAD_READONLY = "1";
		let opened;
		try {
			const { AccountManager } = await import(pathToFileURL(join(packageRoot, "dist/accounts.js")));
			const manager = new AccountManager({ env: process.env });
			opened = await manager.open(accountAlias);
		} catch (error) {
			await handleFailure(error, run, plan, state, undefined);
			return;
		}
		if (!Array.isArray(state.accountOrder) || state.accountOrder.length === 0) {
			try {
				const subscriptions = await opened.canonical.publicAccounts.subscriptions({
					count: 100, offset: 0, signal: AbortSignal.timeout(plan.requestTimeoutMs),
				});
				state.accountOrder = subscriptions.accounts.map((account) => account.accountId);
				state.accountCount = state.accountOrder.length;
				await atomicWrite(run.statePath, state);
				await appendEvent(run.ledgerPath, {
					at: iso(), kind: "subscriptions_success", remoteRequests: 1, accountCount: state.accountCount,
				});
				process.stdout.write(`${JSON.stringify({ status: "subscriptions_initialized", account_count: state.accountCount })}\n`);
				return;
			} catch (error) {
				await handleFailure(error, run, plan, state, undefined);
				return;
			}
		}
		const index = Number(state.cursor || 0) % state.accountOrder.length;
		const accountId = state.accountOrder[index];
		const accountHash = hashId(accountId);
		const priorSyncState = await readJson(join(dataRoot, "sync-state", "state.json"), {});
		const synckey = state.accounts?.[accountHash]?.synckey ?? priorSyncState.accounts?.[accountId]?.synckey;
		try {
			const page = await opened.canonical.publicAccounts.articles(accountId, {
				count: plan.articleRequestCount,
				signal: AbortSignal.timeout(plan.requestTimeoutMs),
				...(synckey === undefined ? {} : { synckey }),
			});
			state.accounts[accountHash] = {
				synckey: page.synckey ?? synckey,
				lastSuccessAt: iso(),
				successCount: Number(state.accounts?.[accountHash]?.successCount || 0) + 1,
			};
			state.cursor = (index + 1) % state.accountOrder.length;
			state.consecutiveSuccesses = Number(state.consecutiveSuccesses || 0) + 1;
			if (state.consecutiveSuccesses >= state.accountOrder.length) state.backoffIndex = 0;
			await atomicWrite(run.statePath, state);
			await appendEvent(run.ledgerPath, {
				at: iso(), kind: "articles_success", remoteRequests: 1, position: index + 1,
				accountHash, returnedCount: Number(page.returnedCount || 0),
			});
			process.stdout.write(`${JSON.stringify({ status: "ok", position: index + 1, account_count: state.accountCount, returned_count: Number(page.returnedCount || 0) })}\n`);
		} catch (error) {
			await handleFailure(error, run, plan, state, { index, accountHash });
		}
	} finally {
		await lock.close().catch(() => undefined);
		await rm(lockPath, { force: true }).catch(() => undefined);
	}
}

async function handleFailure(error, run, plan, state, account) {
	const failure = classify(error);
	const base = { at: iso(), remoteRequests: 1, position: account ? account.index + 1 : undefined };
	if (failure === "human_verification") {
		const index = Math.min(Number(state.backoffIndex || 0), backoffHours.length - 1);
		const hours = backoffHours[index];
		state.blockedUntil = iso(new Date(Date.now() + hours * 60 * 60 * 1000));
		state.backoffIndex = Math.min(index + 1, backoffHours.length - 1);
		state.consecutiveSuccesses = 0;
		await appendEvent(run.ledgerPath, { ...base, kind: "human_verification", backoffHours: hours });
	} else if (failure === "authentication") {
		state.loginRequired = true;
		state.blockedUntil = plan.stopAt;
		await appendEvent(run.ledgerPath, { ...base, kind: "authentication_failure" });
	} else if (failure === "rate_limited") {
		state.blockedUntil = iso(new Date(Date.now() + 60 * 60 * 1000));
		await appendEvent(run.ledgerPath, { ...base, kind: "rate_limited", backoffHours: 1 });
	} else {
		await appendEvent(run.ledgerPath, {
			...base, kind: failure === "transport" ? "transport_failure" : "source_failure",
		});
	}
	await atomicWrite(run.statePath, state);
	process.stdout.write(`${JSON.stringify({ status: failure, blocked_until: state.blockedUntil || null, login_required: Boolean(state.loginRequired) })}\n`);
}

async function status() {
	const run = await loadRun();
	const plan = await readJson(run.planPath, {});
	const state = await readJson(run.statePath, {});
	const events = parseEvents(await readFile(run.ledgerPath, "utf8"));
	await finish(run, plan, state, events, false);
}

const action = process.argv[2] || "step";
if (action === "init") await initialize();
else if (action === "step") await step();
else if (action === "status") await status();
else throw new Error(`unknown action: ${action}`);
