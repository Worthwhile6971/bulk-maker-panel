import test from "node:test"; import assert from "node:assert/strict";
import { address, appendTransactionMessageInstructions, compileTransaction, createTransactionMessage, getBase64EncodedWireTransaction, pipe, setTransactionMessageLifetimeUsingBlockhash } from "@solana/kit";
import { verifyWire, PROGRAM, TRADER_ACCT, IX_DATA_HEX } from "../delegate_tx.mjs";
import { OWNER, DELEGATE } from "../gates.mjs";
const BH = { blockhash: "11111111111111111111111111111111", lastValidBlockHeight: 1n };
const mk = ({ data = IX_DATA_HEX, payer = OWNER, extra = [], accs } = {}) => {
  const ix = { programAddress: address(PROGRAM), data: Buffer.from(data, "hex"), accounts: (accs || [[OWNER, 3], [TRADER_ACCT, 1], [DELEGATE, 0]]).map(([a, r]) => ({ address: address(a), role: r })) };
  const tx = compileTransaction(pipe(createTransactionMessage({ version: 0 }), (t) => ({ ...t, feePayer: { address: address(payer) } }), (t) => setTransactionMessageLifetimeUsingBlockhash(BH, t), (t) => appendTransactionMessageInstructions([ix, ...extra], t)));
  return new Uint8Array(Buffer.from(getBase64EncodedWireTransaction(tx), "base64"));
};
test("valid delegate tx passes", () => assert.equal(verifyWire(mk()).program, PROGRAM));
test("wrong data refused", () => assert.throws(() => verifyWire(mk({ data: "0300000000000000" }))));
test("extra instruction (transfer) refused", () => assert.throws(() => verifyWire(mk({ extra: [{ programAddress: address("11111111111111111111111111111111"), data: new Uint8Array([2, 0, 0, 0]), accounts: [{ address: address(OWNER), role: 3 }, { address: address(DELEGATE), role: 1 }] }] }))));
test("other fee payer refused", () => assert.throws(() => verifyWire(mk({ payer: DELEGATE, accs: [[OWNER, 2], [TRADER_ACCT, 1], [DELEGATE, 0]] }))));
test("missing delegate refused", () => assert.throws(() => verifyWire(mk({ accs: [[OWNER, 3], [TRADER_ACCT, 1]] }))));
