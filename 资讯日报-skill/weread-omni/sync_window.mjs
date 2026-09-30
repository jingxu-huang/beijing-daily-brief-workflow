import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, open, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const dataRoot = resolve(
	process.env.RIBAO_WEREAD_OMNI_DATA_ROOT ||
		join(
			homedir(),
			"Library",
			"Application Support",
			"beijing-daily-brief",
			"weread-omni-test",
		),
);
const packageRoot = resolve(
	process.env.WEREAD_OMNI_PACKAGE_ROOT || join(dataRoot, "source"),
);
const configDir = resolve(
	process.env.WEREAD_CONFIG_DIR || join(dataRoot, "config"),
);
const libraryDir = resolve(
	process.env.WEREAD_LIBRARY_DIR || join(dataRoot, "library"),
);
const stateDir = resolve(
	process.env.RIBAO_WEREAD_OMNI_STATE_DIR || join(dataRoot, "sync-state"),
);
const outputPath = resolve(
	process.env.RIBAO_WEREAD_OMNI_FEED_PATH ||
		join(dataRoot, "weread-omni-test.rss"),
);
const maxPerAccount = boundedInteger(
	process.env.RIBAO_WEREAD_OMNI_MAX_PER_ACCOUNT,
	100,
	1,
	100,
);
const minSyncIntervalMinutes = boundedInteger(
	process.env.RIBAO_WEREAD_OMNI_MIN_SYNC_INTERVAL_MINUTES,
	60,
	10,
	1440,
);
const requestDelayMs = boundedInteger(
	process.env.RIBAO_WEREAD_OMNI_REQUEST_DELAY_MS,
	750,
	0,
	5000,
);
const accountAlias = process.env.WEREAD_ACCOUNT || "default";
const statePath = join(stateDir, "state.json");
const snapshotsDir = join(stateDir, "accounts");
const lockPath = join(stateDir, "sync.lock");

function boundedInteger(value, fallback, minimum, maximum) {
	const parsed = value === undefined ? fallback : Number(value);
	if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
		throw new RangeError(
			`value must be an integer from ${minimum} to ${maximum}`,
		);
	}
	return parsed;
}

function targetDate() {
	const explicit = process.argv.find((value) => /^\d{8}$/.test(value));
	if (explicit) return explicit;
	const parts = new Intl.DateTimeFormat("en-CA", {
		timeZone: "Asia/Shanghai",
		year: "numeric",
		month: "2-digit",
		day: "2-digit",
	}).formatToParts(new Date());
	const value = Object.fromEntries(
		parts.map((part) => [part.type, part.value]),
	);
	return `${value.year}${value.month}${value.day}`;
}

function windowFor(ymd) {
	const year = ymd.slice(0, 4);
	const month = ymd.slice(4, 6);
	const day = ymd.slice(6, 8);
	const end = new Date(`${year}-${month}-${day}T15:00:00+08:00`);
	if (Number.isNaN(end.getTime())) throw new TypeError("invalid target date");
	return { start: new Date(end.getTime() - 24 * 60 * 60 * 1000), end };
}

function articleTime(value) {
	if (!Number.isFinite(value) || value <= 0) return undefined;
	return value < 1_000_000_000_000 ? value * 1000 : value;
}

function json(value) {
	return `${JSON.stringify(value, null, 2)}\n`;
}

async function readJson(path, fallback) {
	try {
		return JSON.parse(await readFile(path, "utf8"));
	} catch (error) {
		if (error?.code === "ENOENT") return fallback;
		throw error;
	}
}

async function atomicWrite(path, contents) {
	await mkdir(dirname(path), { recursive: true, mode: 0o700 });
	const temporary = join(dirname(path), `.${randomUUID()}.tmp`);
	await writeFile(temporary, contents, { mode: 0o600 });
	await rename(temporary, path);
}

function cdata(value) {
	return String(value ?? "").replaceAll("]]>", "]]]]><![CDATA[>");
}

function xmlText(value) {
	return String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&apos;");
}

function canonicalUrl(value) {
	try {
		const url = new URL(value);
		url.hash = "";
		for (const key of [...url.searchParams.keys()]) {
			if (
				/^(?:utm_|from$|scene$|clicktime$|enterid$|sessionid$|subscene$)/i.test(
					key,
				)
			) {
				url.searchParams.delete(key);
			}
		}
		return url.href;
	} catch {
		return String(value ?? "");
	}
}

function rss(items, ymd) {
	const body = items
		.map((item) => {
			const published = new Date(item.date_published);
			return [
				"    <item>",
				`      <title><![CDATA[${cdata(item.title)}]]></title>`,
				`      <link>${xmlText(item.url)}</link>`,
				`      <guid isPermaLink="false">${xmlText(item.id)}</guid>`,
				`      <pubDate>${published.toUTCString()}</pubDate>`,
				`      <dc:creator><![CDATA[${cdata(item._weread_account?.name || "微信公众号")}]]></dc:creator>`,
				`      <content:encoded><![CDATA[${cdata(item.content_html)}]]></content:encoded>`,
				"    </item>",
			].join("\n");
		})
		.join("\n");
	return [
		'<?xml version="1.0" encoding="UTF-8"?>',
		'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">',
		"  <channel>",
		"    <title>微信公众号（weread-omni 测试）</title>",
		"    <link>https://weread.qq.com/</link>",
		`    <description>weread-omni isolated test feed for ${xmlText(ymd)}</description>`,
		...(body ? [body] : []),
		"  </channel>",
		"</rss>",
		"",
	].join("\n");
}

async function recentLimit(client, accountId, startMs) {
	const seen = new Set();
	let requestedOffset = 0;
	let synckey;
	let relevant = 0;
	while (seen.size < maxPerAccount) {
		const page = await client.publicAccounts.articles(accountId, {
			count: Math.min(50, maxPerAccount - seen.size),
			...(requestedOffset === 0 ? {} : { offset: requestedOffset }),
		});
		if (synckey === undefined && page.synckey !== undefined)
			synckey = page.synckey;
		if (page.articles.length === 0) break;
		let oldest;
		for (const article of page.articles) {
			if (typeof article.reviewId !== "string" || seen.has(article.reviewId))
				continue;
			seen.add(article.reviewId);
			const time = articleTime(article.createTime);
			if (time !== undefined) {
				oldest = oldest === undefined ? time : Math.min(oldest, time);
				if (time >= startMs) relevant += 1;
			}
			if (seen.size >= maxPerAccount) break;
		}
		if (oldest !== undefined && oldest < startMs) break;
		if (page.hasMore === 0 || page.nextOffset === undefined) break;
		requestedOffset = page.nextOffset;
	}
	return {
		limit: Math.min(maxPerAccount, Math.max(1, relevant + 3)),
		relevant,
		synckey,
	};
}

function classifyError(error) {
	const status = error?.status;
	const code = error?.errCode;
	const text = `${error?.name ?? ""} ${error?.message ?? ""}`;
	if (code === -2041 || /human verification|official client/i.test(text)) {
		return { kind: "human_verification", exitCode: 7 };
	}
	if (
		status === 401 ||
		code === -2012 ||
		/AuthError|authentication rejected|re-authenticate/i.test(text)
	) {
		return { kind: "authentication", exitCode: 3 };
	}
	if (status === 429 || /rate.?limit|too many requests/i.test(text)) {
		return { kind: "rate_limited", exitCode: 5 };
	}
	return { kind: "upstream_or_transport", exitCode: 4 };
}

function itemsInWindow(items, window) {
	return items.filter((item) => {
		const published = new Date(item.date_published).getTime();
		return (
			Number.isFinite(published) &&
			published >= window.start.getTime() &&
			published < window.end.getTime()
		);
	}).length;
}

async function acquireLock() {
	for (let attempt = 0; attempt < 2; attempt += 1) {
		try {
			const handle = await open(lockPath, "wx", 0o600);
			await handle.writeFile(
				json({ pid: process.pid, startedAt: new Date().toISOString() }),
			);
			return handle;
		} catch (error) {
			if (error?.code !== "EEXIST") throw error;
			const existing = await readJson(lockPath, {});
			const pid = Number(existing.pid);
			let running = Number.isSafeInteger(pid) && pid > 0;
			if (running) {
				try {
					process.kill(pid, 0);
				} catch (probeError) {
					running = probeError?.code === "EPERM";
				}
			}
			if (running || attempt > 0) return undefined;
			await rm(lockPath, { force: true });
		}
	}
	return undefined;
}

await mkdir(stateDir, { recursive: true, mode: 0o700 });
const lock = await acquireLock();
if (!lock) {
	process.stdout.write(
		json({
			source: "weread_omni",
			version: "0.1.1",
			status: "already_running",
		}),
	);
	process.exit(6);
}

let exitCode = 0;
let detectedSubscriptionCount = 0;
try {
	process.env.WEREAD_CONFIG_DIR = configDir;
	process.env.WEREAD_LIBRARY_DIR = libraryDir;
	process.env.WEREAD_READONLY = "1";
	const { AccountManager } = await import(
		pathToFileURL(join(packageRoot, "dist/accounts.js"))
	);
	const { buildPublicAccountFeed, ContentLibrary } = await import(
		pathToFileURL(join(packageRoot, "dist/index.js"))
	);
	const manager = new AccountManager({ env: process.env });
	const opened = await manager.open(accountAlias);
	const subscriptions = await opened.canonical.publicAccounts.subscriptions({
		count: Number.MAX_SAFE_INTEGER,
		offset: 0,
	});
	detectedSubscriptionCount = subscriptions.accounts.length;
	const ymd = targetDate();
	const window = windowFor(ymd);
	const previous = await readJson(statePath, { version: 1, accounts: {} });
	const sameTargetDate = previous.targetDate === ymd;
	const previousRemoteSuccessAt =
		previous.lastRemoteSuccessAt ?? previous.lastSuccessAt;
	const previousRemoteSuccessMs = new Date(
		previousRemoteSuccessAt ?? 0,
	).getTime();
	const remoteRefreshSkipped =
		sameTargetDate &&
		Number.isFinite(previousRemoteSuccessMs) &&
		Date.now() - previousRemoteSuccessMs < minSyncIntervalMinutes * 60 * 1000;
	const nextAccounts = {};
	const combined = [];
	let refreshedSources = 0;
	let reusedSources = 0;
	let staleSources = 0;
	let diagnosticCount = 0;
	let incompleteSources = 0;
	let expectedWindowItems = 0;
	let bodyAvailableWindowItems = 0;
	let unknownExpectedSources = 0;
	let freshSources = 0;
	let freshWindowItems = 0;
	let sourcesWithWindowItems = 0;
	const diagnosticCodes = {};
	const failureClasses = {};
	const library = await ContentLibrary.open({
		vid: opened.identity.vid,
		env: process.env,
	});
	try {
		for (const account of subscriptions.accounts) {
			const snapshotPath = join(
				snapshotsDir,
				`${encodeURIComponent(account.accountId)}.json`,
			);
			const prior = previous.accounts?.[account.accountId];
			let items;
			let synckey = prior?.synckey;
			let health = prior?.health;
			let unchanged = false;
			let sourceFailureKind = "";
			if (remoteRefreshSkipped && existsSync(snapshotPath)) {
				unchanged = true;
			} else if (synckey !== undefined && existsSync(snapshotPath)) {
				try {
					const delta = await opened.canonical.publicAccounts.articles(
						account.accountId,
						{ count: 50, synckey },
					);
					synckey = delta.synckey ?? synckey;
					unchanged = delta.returnedCount === 0;
				} catch (error) {
					const failure = classifyError(error);
					if (
						failure.kind === "authentication" ||
						failure.kind === "human_verification" ||
						failure.kind === "rate_limited"
					)
						throw error;
					sourceFailureKind = failure.kind;
					unchanged = true;
					staleSources += 1;
					diagnosticCount += 1;
					const diagnosticCode =
						sourceFailureKind === "human_verification"
							? "SOURCE_HUMAN_VERIFICATION_REQUIRED"
							: sourceFailureKind === "rate_limited"
								? "SOURCE_RATE_LIMITED_USING_CACHE"
								: "SOURCE_REFRESH_FAILED_USING_CACHE";
					diagnosticCodes[diagnosticCode] =
						(diagnosticCodes[diagnosticCode] ?? 0) + 1;
					failureClasses[sourceFailureKind] =
						(failureClasses[sourceFailureKind] ?? 0) + 1;
				}
			}
			if (unchanged) {
				items = await readJson(snapshotPath, []);
				reusedSources += 1;
				if (sourceFailureKind) {
					health = {
						targetDate: ymd,
						observedAt: new Date().toISOString(),
						expectedWindowItems: null,
						bodyAvailableWindowItems: itemsInWindow(items, window),
						fresh: false,
						failureClass: sourceFailureKind,
					};
				} else if (health?.targetDate !== ymd) {
					// Old snapshots predate the target window. Recompute only what the
					// cache actually contains and do not reuse an older day's expected count.
					health = {
						targetDate: ymd,
						observedAt: new Date().toISOString(),
						expectedWindowItems: null,
						bodyAvailableWindowItems: itemsInWindow(items, window),
						fresh: true,
					};
				}
			} else {
				try {
					const probe = await recentLimit(
						opened.canonical,
						account.accountId,
						window.start.getTime(),
					);
					synckey = probe.synckey ?? synckey;
					const feed = await buildPublicAccountFeed(
						opened.canonical,
						{ kind: "account", accountId: account.accountId },
						{
							format: "json",
							limit: probe.limit,
							library,
							libraryMode: "prefer",
						},
					);
					const parsed = JSON.parse(feed.content);
					items = parsed.items;
					diagnosticCount += feed.diagnostics.length;
					for (const diagnostic of feed.diagnostics) {
						diagnosticCodes[diagnostic.code] =
							(diagnosticCodes[diagnostic.code] ?? 0) + 1;
					}
					const producedInWindow = itemsInWindow(items, window);
					const uniqueCodes = [
						...new Set(feed.diagnostics.map((item) => item.code)),
					];
					health = {
						targetDate: ymd,
						observedAt: new Date().toISOString(),
						expectedWindowItems: probe.relevant,
						bodyAvailableWindowItems: producedInWindow,
						fresh: true,
						diagnosticCodes: Object.fromEntries(
							uniqueCodes.map((code) => [
								code,
								feed.diagnostics.filter((item) => item.code === code).length,
							]),
						),
					};
					await atomicWrite(snapshotPath, json(items));
					refreshedSources += 1;
				} catch (error) {
					const failure = classifyError(error);
					if (
						failure.kind === "authentication" ||
						failure.kind === "human_verification" ||
						failure.kind === "rate_limited" ||
						!existsSync(snapshotPath)
					)
						throw error;
					sourceFailureKind = failure.kind;
					items = await readJson(snapshotPath, []);
					health = {
						targetDate: ymd,
						observedAt: new Date().toISOString(),
						expectedWindowItems: null,
						bodyAvailableWindowItems: itemsInWindow(items, window),
						fresh: false,
						failureClass: sourceFailureKind,
					};
					reusedSources += 1;
					staleSources += 1;
					diagnosticCount += 1;
					const diagnosticCode =
						sourceFailureKind === "human_verification"
							? "SOURCE_HUMAN_VERIFICATION_REQUIRED"
							: sourceFailureKind === "rate_limited"
								? "SOURCE_RATE_LIMITED_USING_CACHE"
								: "SOURCE_REFRESH_FAILED_USING_CACHE";
					diagnosticCodes[diagnosticCode] =
						(diagnosticCodes[diagnosticCode] ?? 0) + 1;
					failureClasses[sourceFailureKind] =
						(failureClasses[sourceFailureKind] ?? 0) + 1;
				}
			}
			combined.push(...items);
			const expected = health?.expectedWindowItems;
			const available = health?.bodyAvailableWindowItems ?? 0;
			if (Number.isFinite(expected)) expectedWindowItems += expected;
			else unknownExpectedSources += 1;
			bodyAvailableWindowItems += available;
			if (available > 0) sourcesWithWindowItems += 1;
			if (!sourceFailureKind) {
				freshSources += 1;
				freshWindowItems += available;
			}
			if (Number.isFinite(expected) && available < expected)
				incompleteSources += 1;
			if (unchanged && !sourceFailureKind) {
				for (const [code, count] of Object.entries(
					health?.diagnosticCodes ?? {},
				)) {
					diagnosticCodes[code] = (diagnosticCodes[code] ?? 0) + count;
					diagnosticCount += count;
				}
			}
			nextAccounts[account.accountId] = {
				synckey,
				snapshot: snapshotPath,
				health,
			};
			if (requestDelayMs > 0) await delay(requestDelayMs);
		}
		const verification = await library.verify();
		if (!verification.ok)
			throw new Error("local content library verification failed");
	} finally {
		library.close();
	}

	const seenIds = new Set();
	const seenUrls = new Set();
	const items = [];
	for (const item of combined) {
		const published = new Date(item.date_published).getTime();
		if (
			!Number.isFinite(published) ||
			published < window.start.getTime() ||
			published >= window.end.getTime()
		)
			continue;
		const id = String(item.id ?? "");
		const url = canonicalUrl(item.url);
		if ((!id && !url) || (id && seenIds.has(id)) || (url && seenUrls.has(url)))
			continue;
		if (id) seenIds.add(id);
		if (url) seenUrls.add(url);
		items.push({ ...item, url });
	}
	items.sort(
		(left, right) =>
			new Date(right.date_published).getTime() -
			new Date(left.date_published).getTime(),
	);
	await atomicWrite(outputPath, rss(items, ymd));
	const completedAt = new Date().toISOString();
	const lastRemoteSuccessAt =
		staleSources === 0 && !remoteRefreshSkipped
			? completedAt
			: previousRemoteSuccessAt;
	await atomicWrite(
		statePath,
		json({
			version: 1,
			source: "weread_omni",
			sourceVersion: "0.1.1",
			upstreamCommit: "3a7fee58995ef502f2044d93f11af7b76fdacd0e",
			targetDate: ymd,
			lastSuccessAt: lastRemoteSuccessAt,
			lastRemoteSuccessAt,
			lastCompletedAt: completedAt,
			accounts: nextAccounts,
		}),
	);
	process.stdout.write(
		json({
			source: "weread_omni",
			version: "0.1.1",
			status:
				(failureClasses.human_verification ?? 0) > 0
					? "human_verification_required"
					: remoteRefreshSkipped && refreshedSources === 0 && staleSources === 0
					? "cached"
					: incompleteSources === 0 && staleSources === 0
						? "healthy"
						: "partial",
			subscriptionCount: subscriptions.accounts.length,
			refreshedSources,
			reusedSources,
			staleSources,
			freshSources,
			remoteRefreshSkipped,
			windowItemCount: items.length,
			freshWindowItemCount: freshWindowItems,
			sourcesWithWindowItems,
			expectedWindowItems,
			bodyAvailableWindowItems,
			unknownExpectedSources,
			diagnosticCount,
			diagnosticCodes,
			failureClasses,
			incompleteSources,
			authentication: "authenticated",
			targetDate: ymd,
			completedAt,
			lastRemoteSuccessAt,
		}),
	);
	if ((failureClasses.human_verification ?? 0) > 0) exitCode = 7;
	else if (incompleteSources > 0 || staleSources > 0) exitCode = 5;
} catch (error) {
	const classification = classifyError(error);
	process.stdout.write(
		json({
			source: "weread_omni",
			version: "0.1.1",
			status:
				classification.kind === "human_verification"
					? "human_verification_required"
					: classification.kind === "rate_limited"
						? "rate_limited"
						: "failed",
			failureClass: classification.kind,
			targetDate: targetDate(),
			subscriptionCount: detectedSubscriptionCount || undefined,
		}),
	);
	exitCode = classification.exitCode;
} finally {
	await lock?.close().catch(() => undefined);
	await rm(lockPath, { force: true }).catch(() => undefined);
}

process.exitCode = exitCode;
