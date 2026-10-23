// Builds a FRESH UNSIGNED DelegateTrader tx (owner tr3H..kGf signs in Solflare) or reads sub0 authority. Holds NO key, never sends.
// Usage: node delegate_tx.mjs build | authority     -> JSON on stdout
import { createPhoenixClient, decodeTrader } from "@ellipsis-labs/rise";
import { address, appendTransactionMessageInstructions, compileTransaction, createSolanaRpc, createTransactionMessage,
  getBase64EncodedWireTransaction, pipe, setTransactionMessageLifetimeUsingBlockhash, getCompiledTransactionMessageDecoder, getTransactionDecoder } from "@solana/kit";
import { OWNER, DELEGATE } from "./gates.mjs";
export const PROGRAM = "EtrnLzgbS7nMMy5fbD42kXiUzGg8XQzJ972Xtk1cjWih";
export const TRADER_ACCT = "Cf8i8GyNfDTdBZc4KAmiFzHTYaSMNiDxuQFastRV2w9n";
export const IX_DATA_HEX = "03a4c06a775b7ae0";
export const ALLOWED = new Set([PROGRAM, "GdxfTLSsdSY37G6fZoYtdGDSfgFnbT2EmRpuePZxWShS", "2zskx2iyCvb6Stg7RBZkt1f6MrF4dpYtMG3yMvKwqtUZ", OWNER, TRADER_ACCT, DELEGATE]);
const RPC_URL = process.env.PHOENIX_RPC_URL || "https://api.mainnet-beta.solana.com";
const API = process.env.PHOENIX_API || "https://perp-api.phoenix.trade";
// Throws unless: v0, no lookup tables, exactly 1 signer = owner (fee payer), exactly 1 ix = Phoenix DelegateTrader with expected data, delegate present, only allow-listed accounts.
export function verifyWire(wire) {
  const tx = getTransactionDecoder().decode(wire);
  const m = getCompiledTransactionMessageDecoder().decode(tx.messageBytes);
  const keys = m.staticAccounts.map(String);
  if (m.addressTableLookups && m.addressTableLookups.length) throw new Error("lookup tables present");
  if (m.header.numSignerAccounts !== 1 || keys[0] !== OWNER) throw new Error("signer set is not exactly the owner");
  if (m.instructions.length !== 1) throw new Error("expected exactly 1 instruction, got " + m.instructions.length);
  const ix = m.instructions[0];
  if (keys[ix.programAddressIndex] !== PROGRAM) throw new Error("program is not Phoenix");
  if (Buffer.from(ix.data ?? []).toString("hex") !== IX_DATA_HEX) throw new Error("unexpected instruction data");
  const acc = (ix.accountIndices ?? []).map((i) => keys[i]);
  if (!acc.includes(DELEGATE) || !acc.includes(OWNER)) throw new Error("owner/delegate missing from instruction");
  for (const k of keys) if (!ALLOWED.has(k)) throw new Error("unexpected account " + k);
  return { program: PROGRAM, accounts: acc, signers: [OWNER], data: IX_DATA_HEX };
}
export async function authority() {
  const a = await createSolanaRpc(RPC_URL).getAccountInfo(address(TRADER_ACCT), { encoding: "base64", commitment: "confirmed" }).send();
  const t = decodeTrader(Buffer.from(a.value.data[0], "base64"));
  const pa = String(t.positionAuthority);
  return { traderAccount: TRADER_ACCT, authority: String(t.authority), positionAuthority: pa, isDelegate: pa === DELEGATE, delegate: DELEGATE, subaccount: Number(t.traderSubaccountIndex) };
}
export async function build() {
  const rpc = createSolanaRpc(RPC_URL);
  const client = createPhoenixClient({ apiUrl: API, rpcUrl: RPC_URL, ws: false, exchangeMetadata: { stream: false } });
  const ix = await client.ixs.buildDelegateTrader({ traderWallet: address(OWNER), traderPdaIndex: 0, traderSubaccountIndex: 0, newPositionAuthority: address(DELEGATE) });
  const bh = await rpc.getLatestBlockhash({ commitment: "confirmed" }).send();
  const msg = pipe(createTransactionMessage({ version: 0 }), (t) => ({ ...t, feePayer: { address: address(OWNER) } }),
    (t) => setTransactionMessageLifetimeUsingBlockhash(bh.value, t), (t) => appendTransactionMessageInstructions([ix], t));
  const tx = compileTransaction(msg);
  const b64 = getBase64EncodedWireTransaction(tx);
  const check = verifyWire(new Uint8Array(Buffer.from(b64, "base64")));
  const sim = await rpc.simulateTransaction(b64, { encoding: "base64", commitment: "confirmed", sigVerify: false }).send();
  const fee = await rpc.getFeeForMessage(Buffer.from(tx.messageBytes).toString("base64"), { commitment: "confirmed" }).send().catch(() => null);
  return { base64: b64, check, blockhash: bh.value.blockhash, lastValidBlockHeight: String(bh.value.lastValidBlockHeight),
    simulation: { err: sim.value.err ? JSON.parse(JSON.stringify(sim.value.err, (_, v) => typeof v === "bigint" ? String(v) : v)) : null, logs: (sim.value.logs || []).slice(-3) },
    feeLamports: fee?.value != null ? String(fee.value) : "5000" };
}
if (process.argv[1]?.endsWith("delegate_tx.mjs")) {
  const cmd = process.argv[2];
  try { console.log(JSON.stringify(cmd === "authority" ? await authority() : cmd === "build" ? await build() : (() => { throw new Error("usage"); })())); }
  catch (e) { console.log(JSON.stringify({ error: String(e.message || e).slice(0, 300) })); process.exit(1); }
}
