#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { verifyProofBundle } from '../buyer/gateway.mjs';

const args = process.argv.slice(2);
const tamper = args[0] === '--tamper';
const [bundlePath, keysPath, ...extra] = tamper ? args.slice(1) : args;
if (!bundlePath || !keysPath || extra.length) {
  console.error('Usage: node scripts/verify_buyer_bundle.mjs [--tamper] bundle.json trusted-jwks.json');
  process.exitCode = 1;
} else {
  try {
    const [bundle, keys] = await Promise.all([bundlePath, keysPath].map(async path =>
      JSON.parse(await readFile(path, 'utf8'))));
    const receipt = await verifyProofBundle(bundle, keys);
    if (tamper) {
      const [header, payload, encodedSignature] = bundle.receipt_jws.split('.');
      const signature = Buffer.from(encodedSignature, 'base64url');
      signature[0] ^= 1;
      const altered = { ...bundle, receipt_jws: `${header}.${payload}.${signature.toString('base64url')}` };
      let rejection;
      try {
        await verifyProofBundle(altered, keys);
      } catch (error) {
        rejection = error.message;
      }
      if (!rejection) throw new Error('The altered receipt was unexpectedly accepted.');
      console.log(JSON.stringify({
        original_verified: true, order_id: receipt.id, tamper_rejected: true,
        changed_document: 'receipt signature', reason: rejection, trusted_keys_file: keysPath,
      }, null, 2));
    } else {
      console.log(JSON.stringify({
        verified: true, order_id: receipt.id, status: receipt.status,
        total_ex_vat: receipt.total_ex_vat, amendments: receipt.amendments?.length ?? 0,
        trusted_keys_file: keysPath,
      }, null, 2));
    }
  } catch (error) {
    console.error(`Verification failed: ${error.message}`);
    process.exitCode = 1;
  }
}
