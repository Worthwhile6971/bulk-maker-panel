// Phoenix signer sidecar. 127.0.0.1 only, internal token required, delegate key ONLY (env PHOENIX_DELEGATE_KEY).
// Accepts: post-only entries, reduce-only post-only closes, reduce-only position stop-loss, cancels. Nothing else.
// mode=simulate: builds + signs with the delegate + simulateTransaction (never broadcast).
// mode=send: additionally requires approved:true, PHOENIX_HALT=0, PHOENIX_TRADING_ENABLED=1, and on-chain sub0 positionAuthority == delegate.
import http from "node:http";
import { base58 } from "@scure/base";
import { OrderFlags, Side, Direction, StopLossOrderKind, createPhoenixClient, decodeTrader,
  priceUsdToTicksWithMarketParams, ticksToUsdWithMarketParams, baseUnitsToBaseLotsWithMarketParams } from "@ellipsis-labs/rise";
import { address, appendTransactionMessageInstructions, createSolanaRpc, createTransactionMessage, getBase64EncodedWireTransaction, pipe,
  setTransactionMessageLifetimeUsingBlockhash, getSignatureFromTransaction } from "@solana/kit";
import { addSignersToInstruction, createKeyPairSignerFromBytes, setTransactionMessageFeePayerSigner, signTransactionMessageWithSigners } from "@solana/signers";
import { DELEGATE, OWNER, envGate, validateRequest, postOnlyNoCross, authorityGate } from "./gates.mjs";

const API = process.env.PHOENIX_API || "https://perp-api.phoenix.trade";
const RPC_URL = process.env.PHOENIX_RPC_URL || "https://api.mainnet-beta.solana.com";
const TRADER_ACCT = "Cf8i8GyNfDTdBZc4KAmiFzHTYaSMNiDxuQFastRV2w9n"; // owner, pda 0, sub 0
const PORT = Number(process.env.PHX_SIGNER_PORT || 8799), TOKEN = process.env.PHX_SIGNER_TOKEN || "";
const SECRET = (process.env.PHOENIX_DELEGATE_KEY || "").trim();
const redact = (e) => { let t = e instanceof Error ? e.message : String(e); for (const s of [SECRET, process.env.PHOENIX_RPC_URL || ""]) if (s) t = t.split(s).join("[redacted]"); return t; };
const dump = (x) => JSON.parse(JSON.stringify(x, (_, v) => (typeof v === "bigint" ? v.toString() : v)));
const rpc = createSolanaRpc(RPC_URL);
const client = createPhoenixClient({ apiUrl: API, rpcUrl: RPC_URL, ws: false, exchangeMetadata: { stream: false } });
const getJson = async (p) => { const r = await fetch(API + p, { signal: AbortSignal.timeout(10000) }); if (!r.ok) throw new Error("Phoenix API HTTP " + r.status); return r.json(); };

let SIGNER = null;
export async function signer() {
  if (SIGNER) return SIGNER;
  if (!SECRET) throw new Error("PHOENIX_DELEGATE_KEY not set");
  const bytes = SECRET.startsWith("[") ? Uint8Array.from(JSON.parse(SECRET)) : base58.decode(SECRET);
  if (bytes.length !== 64) throw new Error("PHOENIX_DELEGATE_KEY must be a 64-byte keypair");
  const s = await createKeyPairSignerFromBytes(bytes);
  if (s.address !== DELEGATE) throw new Error("PHOENIX_DELEGATE_KEY pubkey is not the delegate B76z..vJg: refusing");
  return (SIGNER = s);
}
const market = async (sym) => { const m = (await getJson("/v1/view/exchange/markets")).find((x) => x.symbol === sym); if (!m || m.marketStatus !== "active") throw new Error("market not active: " + sym); return m; };
const mp = (m) => ({ tickSize: m.tickSize, baseLotsDecimals: m.baseLotsDecimals });
const touch = async (sym) => { const b = await getJson("/v1/view/orderbook/" + sym); return { bid: Number(b.bids[0][0]), ask: Number(b.asks[0][0]) }; };
export async function onchainAuthority() {
  const a = await rpc.getAccountInfo(address(TRADER_ACCT), { encoding: "base64", commitment: "confirmed" }).send();
  const t = decodeTrader(Buffer.from(a.value.data[0], "base64"));
  return { authority: String(t.authority), positionAuthority: String(t.positionAuthority) };
}
const subState = async () => ((await getJson(`/v1/trader/state/${OWNER}?traderPdaIndex=0`)).snapshot.subaccounts || []).find((s) => s.subaccountIndex === 0) || {};
const base = (sym) => ({ authority: address(OWNER), positionAuthority: address(DELEGATE), symbol: sym, traderPdaIndex: 0, traderSubaccountIndex: 0 });
const packet = (isBuy, t, lots, reduceOnly) => ({ side: isBuy ? Side.Bid : Side.Ask, priceInTicks: t, numBaseLots: lots, clientOrderId: BigInt(Date.now()) % 1_000_000_000n,
  slide: false, lastValidSlot: null, orderFlags: reduceOnly ? OrderFlags.ReduceOnly : OrderFlags.None, cancelExisting: false });

async function buildIxs(b) {
  const sym = b.symbol; const m = await market(sym);
  if (b.kind === "cancel") return { ixs: [await client.ixs.buildCancelOrdersById({ ...base(sym), orders: b.orders.map((o) => ({ priceInTicks: BigInt(o.priceInTicks), orderSequenceNumber: BigInt(o.orderSequenceNumber) })) })], plan: { cancel: b.orders.length } };
  if (b.kind === "open") {
    const isBuy = b.side === "long", { bid, ask } = await touch(sym);
    const t = BigInt(priceUsdToTicksWithMarketParams(Number(b.price), mp(m))); const px = ticksToUsdWithMarketParams(t, mp(m));
    postOnlyNoCross(isBuy, px, bid, ask);
    const lots = BigInt(baseUnitsToBaseLotsWithMarketParams(String(Number(b.size)), mp(m))); if (lots <= 0n) throw new Error("size rounds to 0 lots");
    const ix = await client.ixs.buildPlacePostOnlyOrder({ ...base(sym), orderPacket: packet(isBuy, t, lots, false) });
    return { ixs: [ix], plan: { symbol: sym, side: b.side, price: px, lots: String(lots), sl: Number(b.sl), postOnly: true, reduceOnly: false, book: { bid, ask },
      note: "stop-loss is placed as a reduce-only position stop (kind=protect) once the entry fills" } };
  }
  const pos = (await subState()).positions?.find((p) => p.symbol === sym && BigInt(p.basePositionLots) !== 0n);
  const hyp = b.hypothetical; if (!pos && !hyp) throw new Error("no " + sym + " position on sub0");
  const lotsAll = pos ? BigInt(pos.basePositionLots) : BigInt(baseUnitsToBaseLotsWithMarketParams(String(hyp.size), mp(m))) * (hyp.side === "short" ? -1n : 1n);
  const isLong = lotsAll > 0n, abs = isLong ? lotsAll : -lotsAll;
  if (b.kind === "close") {
    const { bid, ask } = await touch(sym); const isBuy = !isLong;
    const t = BigInt(priceUsdToTicksWithMarketParams(isBuy ? bid : ask, mp(m))); postOnlyNoCross(isBuy, ticksToUsdWithMarketParams(t, mp(m)), bid, ask);
    const lots = (abs * BigInt(Math.round(Number(b.percent) * 100))) / 10000n;
    return { ixs: [await client.ixs.buildPlacePostOnlyOrder({ ...base(sym), orderPacket: packet(isBuy, t, lots, true) })], plan: { close: isLong ? "LONG" : "SHORT", lots: String(lots), postOnly: true, reduceOnly: true } };
  }
  // protect: reduce-only position stop (IOC on trigger), 100% of position
  const sl = Number(b.sl), st = BigInt(priceUsdToTicksWithMarketParams(sl, mp(m)));
  const leg = { triggerDirection: isLong ? Direction.LessThan : Direction.GreaterThan, tradeSide: isLong ? Side.Ask : Side.Bid, orderKind: StopLossOrderKind.IOC, triggerPrice: st, executionPrice: st };
  const ix = await client.ixs.buildPlacePositionConditionalOrder({ ...base(sym), payer: address(DELEGATE), sizePercent: 100, [isLong ? "lessTriggerOrder" : "greaterTriggerOrder"]: leg });
  return { ixs: [ix], plan: { stop: sl, side: isLong ? "LONG" : "SHORT", reduceOnly: true, sizePercent: 100 } };
}

export async function handle(b, deps = {}) {
  validateRequest(b);
  const env = deps.env || process.env;
  if (b.mode === "send") {
    const g = envGate(env); if (!g.ok) return { sent: false, refused: g.reasons };
    authorityGate(await (deps.onchainAuthority || onchainAuthority)());
  }
  const s = await signer();
  const { ixs, plan } = await buildIxs(b);
  const bh = await rpc.getLatestBlockhash({ commitment: "confirmed" }).send();
  const cu = new Uint8Array(5); cu[0] = 2; new DataView(cu.buffer).setUint32(1, 600_000, true);   // SetComputeUnitLimit only (no price)
  const cuIx = { programAddress: address("ComputeBudget111111111111111111111111111111"), accounts: [], data: cu };
  const msg = pipe(createTransactionMessage({ version: 0 }), (t) => setTransactionMessageFeePayerSigner(s, t),
    (t) => setTransactionMessageLifetimeUsingBlockhash(bh.value, t), (t) => appendTransactionMessageInstructions([cuIx, ...ixs.map((ix) => addSignersToInstruction([s], ix))], t));
  const signed = await signTransactionMessageWithSigners(msg);
  const wire = getBase64EncodedWireTransaction(signed);
  const sim = await rpc.simulateTransaction(wire, { encoding: "base64", commitment: "processed", sigVerify: true }).send();
  const out = { kind: b.kind, mode: b.mode, plan: dump(plan), signer: DELEGATE, simulation: { err: sim.value.err ? dump(sim.value.err) : null, logs: (sim.value.logs || []).slice(-10).map(redact), units: String(sim.value.unitsConsumed ?? "") }, sent: false };
  if (b.mode !== "send") return out;
  if (out.simulation.err) return { ...out, refused: ["simulation failed; not sending"] };
  const sig = await rpc.sendTransaction(wire, { encoding: "base64", skipPreflight: false }).send(); // live path: reachable only past every gate above
  return { ...out, sent: true, signature: String(sig || getSignatureFromTransaction(signed)) };
}

export const health = async () => { const g = envGate(process.env); let delegate = null, err = null; try { delegate = (await signer()).address; } catch (e) { err = redact(e); }
  return { ok: true, live: g.ok, refused: g.reasons, delegate, keyError: err, node: process.version }; };

if (process.argv[1] && process.argv[1].endsWith("signer.mjs")) {
  if (!TOKEN) { console.error("PHX_SIGNER_TOKEN required"); process.exit(1); }
  http.createServer(async (req, res) => {
    const send = (c, o) => { res.writeHead(c, { "content-type": "application/json" }); res.end(JSON.stringify(o)); };
    if (req.headers["x-signer-token"] !== TOKEN) return send(401, { error: "unauthorized" });
    try {
      if (req.url === "/health") return send(200, await health());
      if (req.method === "POST" && req.url === "/order") { let s = ""; for await (const c of req) { s += c; if (s.length > 20000) throw new Error("too large"); } return send(200, await handle(JSON.parse(s || "{}"))); }
      send(404, { error: "not found" });
    } catch (e) { send(400, { error: redact(e) }); }
  }).listen(PORT, "127.0.0.1");
}
