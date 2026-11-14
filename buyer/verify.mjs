import { verifyProofBundle } from './gateway.mjs';

const form = document.getElementById('verify-form');
const result = document.getElementById('verification-result');

function show(title, text, verified = false) {
  const heading = document.createElement('h2');
  heading.textContent = title;
  const message = document.createElement('p');
  message.textContent = text;
  message.className = verified ? 'verified' : 'notice';
  result.replaceChildren(heading, message);
  result.hidden = false;
}

async function verify(tamper = false) {
  if (!form.reportValidity()) return;
  for (const control of form.querySelectorAll('button, input')) control.disabled = true;
  result.hidden = true;
  try {
    const files = ['proof-file', 'keys-file'].map(id => document.getElementById(id).files[0]);
    const [bundle, keys] = await Promise.all(files.map(async file => JSON.parse(await file.text())));
    const receipt = await verifyProofBundle(bundle, keys);
    if (tamper) {
      const [header, payload, encoded] = bundle.receipt_jws.split('.');
      const signature = atob(encoded.replaceAll('-', '+').replaceAll('_', '/'));
      const altered = String.fromCharCode(signature.charCodeAt(0) ^ 1) + signature.slice(1);
      const changed = btoa(altered).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
      try {
        await verifyProofBundle({ ...bundle, receipt_jws: `${header}.${payload}.${changed}` }, keys);
      } catch {
        show('Altered signature rejected', `The original proof for ${receipt.id} verifies. Changing one signature byte fails verification. Your files are unchanged.`, true);
        return;
      }
      throw new Error('The altered receipt was unexpectedly accepted.');
    }
    show('Proof verified', `${receipt.id} · signed status: ${receipt.status} · ${receipt.total_ex_vat} ex VAT · ${receipt.amendments?.length ?? 0} signed amendments.`, true);
  } catch (error) {
    show('Proof could not be verified', error.message);
  } finally {
    for (const control of form.querySelectorAll('button, input')) control.disabled = false;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  verify();
});
document.getElementById('tamper-proof').addEventListener('click', () => verify(true));
form.addEventListener('change', () => { result.hidden = true; });
