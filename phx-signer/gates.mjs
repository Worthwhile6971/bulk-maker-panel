// Pure gate logic for the Phoenix signer (unit-tested). No I/O, no keys.
export const DELEGATE = "B76zNWw7n45c65Rv3spyccMjr9VqkzWSdyRqsa8HXvJg";
export const OWNER = "tr3H9VuLVv9VsccPTcv7YJHEomDKZZPhULSV4bzkkGf";
export const KINDS = new Set(["open", "close", "protect", "cancel"]);
export function envGate(env) {
  const reasons = [];
  if (env.PHOENIX_HALT !== "0") reasons.push("Phoenix halt on (" + (env.PHOENIX_HALT ?? "default") + ")");
  if (env.PHOENIX_TRADING_ENABLED !== "1") reasons.push("PHOENIX_TRADING_ENABLED not set");
  if (!env.PHOENIX_DELEGATE_KEY) reasons.push("PHOENIX_DELEGATE_KEY not set");
  if ("PHOENIX_TRADER_PRIVATE_KEY" in env) reasons.push("owner key present in signer env: refusing");
  return { ok: reasons.length === 0, reasons };
}
// Validate an order request. Only maker post-only entries, reduce-only post-only closes,
// reduce-only position stops, and cancels. Throws on anything else.
export function validateRequest(b) {
  if (!b || typeof b !== "object") throw new Error("bad body");
  if (!KINDS.has(b.kind)) throw new Error("kind must be open|close|protect|cancel");
  if (b.mode !== "simulate" && b.mode !== "send") throw new Error("mode must be simulate|send");
  if (b.mode === "send" && b.approved !== true) throw new Error("explicit approval required (approved: true)");
  for (const k of ["market", "ioc", "fok", "slide", "takerOk"]) if (b[k]) throw new Error(`${k} orders are not allowed (maker-only)`);
  if (!/^[A-Z0-9]{1,12}$/.test(String(b.symbol || ""))) throw new Error("bad symbol");
  if (b.kind === "open") {
    if (b.side !== "long" && b.side !== "short") throw new Error("side must be long|short");
    const px = Number(b.price), sl = Number(b.sl), sz = Number(b.size);
    if (!(px > 0) || !(sz > 0)) throw new Error("price and size required");
    if (!(sl > 0)) throw new Error("stop-loss is required");
    if (b.side === "long" ? sl >= px : sl <= px) throw new Error("stop-loss must be beyond entry");
  }
  if (b.kind === "protect" && !(Number(b.sl) > 0)) throw new Error("protect needs sl");
  if (b.kind === "close" && !(Number(b.percent) > 0 && Number(b.percent) <= 100)) throw new Error("percent 1-100");
  if (b.kind === "cancel" && !(Array.isArray(b.orders) && b.orders.length)) throw new Error("cancel needs orders");
  return true;
}
export function postOnlyNoCross(isBuy, px, bid, ask) {
  if (isBuy && !(px < ask)) throw new Error(`post-only bid ${px} would cross ask ${ask}`);
  if (!isBuy && !(px > bid)) throw new Error(`post-only ask ${px} would cross bid ${bid}`);
}
export function authorityGate(onchain) {
  if (onchain.authority !== OWNER) throw new Error("trader owner mismatch");
  if (onchain.positionAuthority !== DELEGATE) throw new Error(`sub0 position authority is ${onchain.positionAuthority}, not delegate ${DELEGATE}`);
}
