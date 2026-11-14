#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { verifyProofBundle } from '../buyer/gateway.mjs';

const [bundlePath, keysPath, ...extra] = process.argv.slice(2);
if (!bundlePath || !keysPath || extra.length) {
  console.error('Usage: node scripts/verify_buyer_bundle.mjs bundle.json trusted-jwks.json');
  process.exitCode = 1;
} else {
  try {
    const [bundle, keys] = await Promise.all([bundlePath, keysPath].map(async path =>
      JSON.parse(await readFile(path, 'utf8'))));
    const receipt = await verifyProofBundle(bundle, keys);
    console.log(JSON.stringify({
      verified: true, order_id: receipt.id, status: receipt.status,
      total_ex_vat: receipt.total_ex_vat, amendments: receipt.amendments?.length ?? 0,
      trusted_keys_file: keysPath,
    }, null, 2));
  } catch (error) {
    console.error(`Verification failed: ${error.message}`);
    process.exitCode = 1;
  }
}
