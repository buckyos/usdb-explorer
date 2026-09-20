import React from 'react';

import { ErrorNotice, PageHeader } from './shared';

import styles from './usdb.module.css';

type Status = {
  enabled: boolean; status: string; address?: string; claim_amount?: string; daily_budget?: string;
  cooldown_seconds?: number; confirmations?: number;
};
type Claim = { id: string; address: string; status: string; transaction_hash?: string };
type Application = { id: string; address: string };

const messages: Record<string, string> = {
  STARTING: 'The faucet is starting. Please try again shortly.',
  INSUFFICIENT_FUNDS: 'The faucet needs a refill. Please try again after the operator adds funds.',
  DAILY_BUDGET_EXHAUSTED: 'This faucet has reached its daily budget. Please try again after the next UTC day begins.',
  ADDRESS_COOLDOWN: 'This address has claimed recently. Please wait for the address cooldown to expire.',
  ADDRESS_PENDING: 'This address already has an application waiting for confirmation.',
  IP_RATE_LIMIT: 'Too many requests from your network. Please wait a minute before retrying.',
  IP_DAILY_LIMIT: 'Your network has reached its rolling 24-hour claim limit. Please try again later.',
  QUEUE_FULL: 'The faucet queue is full. Please try again later.',
  INVALID_ADDRESS: 'Enter a valid USDB wallet address, including the 0x prefix and correct checksum.',
  WRONG_NETWORK: 'The faucet is paused because the upstream network does not match. The operator needs to check it.',
  GAS_PRICE_TOO_HIGH: 'The faucet is paused while transaction fees exceed its configured limit.',
  NODE_SYNCING: 'The USDB node is synchronizing. Please try again later.',
  BROADCAST_UNCERTAIN: 'The node has not confirmed the broadcast result. Your existing application will be checked again.',
  ORIGIN_MISMATCH: 'This page address does not match the configured explorer URL. Ask the operator to check the public URL.',
};
const progress: Record<string, string> = {
  queued: 'Queued for transfer', pending: 'Awaiting inclusion in a block', confirming: 'Waiting for confirmations',
  confirmed: 'Test USDB delivered', failed: 'The transfer failed on-chain. Contact the operator before requesting again.',
  rejected: 'This recipient is a contract. This faucet currently supports ordinary wallet addresses only.',
};
const storageKey = 'usdb-faucet-application-v1';

function message(code: string): string {
  return messages[code] || 'The faucet is temporarily unavailable. Keep your application and try again later.';
}

async function request<T>(path: string, application?: Application): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('/api/faucet/v1/' + path, {
      method: application ? 'POST' : 'GET', credentials: 'omit', cache: 'no-store', signal: controller.signal,
      ...(application ? { headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: application.id, address: application.address }) } : {}),
    });
    if (response.status === 404 && path.startsWith('claims/')) {
      throw new Error('Application not found yet. Retry the same application to submit it safely.');
    }
    const value = await response.json();
    if (!value || typeof value !== 'object') { throw new Error(message('')); }
    const envelope = value as { error?: { code?: string } };
    if (!response.ok) { throw new Error(message(envelope.error?.code || '')); }
    return value as T;
  } catch (error) {
    if (error instanceof Error && error.name !== 'AbortError' && error.name !== 'TypeError' && error.name !== 'SyntaxError') { throw error; }
    throw new Error('Connection interrupted. Retry the same application; a previous submission may already be queued.');
  } finally { clearTimeout(timeout); }
}

export default function Faucet() {
  const [ status, setStatus ] = React.useState<Status>();
  const [ address, setAddress ] = React.useState('');
  const [ application, setApplication ] = React.useState<Application>();
  const [ claim, setClaim ] = React.useState<Claim>();
  const [ error, setError ] = React.useState('');
  const [ busy, setBusy ] = React.useState(false);
  const applicationRef = React.useRef<Application | undefined>(undefined);

  React.useEffect(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null') as Partial<Application> | null;
      if (saved && typeof saved.id === 'string' && typeof saved.address === 'string' &&
        /^[a-f0-9]{32}$/.test(saved.id) && /^0x[a-fA-F0-9]{40}$/.test(saved.address)) {
        const restored = { id: saved.id, address: saved.address };
        applicationRef.current = restored; setApplication(restored); setAddress(restored.address);
      }
    } catch { /* Private browsing may disable storage; server-side idempotency still applies. */ }
  }, []);

  React.useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async() => {
      try {
        const value = await request<Status>('status');
        if (active) { setStatus(value); }
        const current = applicationRef.current;
        if (value.enabled && current) {
          const result = await request<Claim>('claims/c_' + current.id);
          if (active && applicationRef.current?.id === current.id) { setClaim(result); setError(''); }
        }
      } catch (error) {
        if (active) {
          const detail = (error as Error).message;
          setError(previous => detail.startsWith('Application not found') ? previous || detail : detail);
        }
      }
      finally { if (active) { timer = setTimeout(refresh, 10000); } }
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  const submit = async(event: React.FormEvent) => {
    event.preventDefault();
    if (!/^0x[a-fA-F0-9]{40}$/.test(address.trim())) { setError(message('INVALID_ADDRESS')); return; }
    let current = applicationRef.current;
    if (!current) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      current = { id: Array.from(bytes, value => value.toString(16).padStart(2, '0')).join(''), address: address.trim() };
      applicationRef.current = current; setApplication(current);
      // Save before sending so a reload after a lost response can recover the same request.
      try { sessionStorage.setItem(storageKey, JSON.stringify(current)); } catch { /* Storage is optional. */ }
    }
    setBusy(true); setError('');
    try { setClaim(await request<Claim>('claims', current)); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };

  const reset = () => {
    applicationRef.current = undefined; setApplication(undefined); setClaim(undefined); setError(''); setAddress('');
    try { sessionStorage.removeItem(storageKey); } catch { /* Storage is optional. */ }
  };

  return <section className={ styles.root }>
    <PageHeader title="Testnet faucet" description="Receive test USDB for transactions and application testing."/>
    { !status && !error && <p role="status">Checking faucet availability…</p> }
    { status && !status.enabled && <p className={ styles.notice } role="status">This explorer does not currently offer a faucet.</p> }
    { status?.enabled && <>
      <div className={ styles.cards }>
        <article><h2>Per claim</h2><strong>{ status.claim_amount } USDB</strong><p>Fixed amount for each accepted application</p></article>
        <article><h2>Address cooldown</h2><strong>{ (status.cooldown_seconds || 0) / 3600 } hours</strong><p>Applies to this faucet</p></article>
        <article><h2>Daily budget</h2><strong>{ status.daily_budget } USDB</strong><p>UTC · includes reserved maximum transaction fees</p></article>
        <article><h2>Confirmations</h2><strong>{ status.confirmations }</strong><p>Required before a transfer is marked delivered</p></article>
      </div>
      { status.status !== 'ready' && <p className={ styles.notice } role="status">{ message(status.status) }</p> }
      <div className={ styles.panel }>
        <h2>Receive test USDB</h2>
        <p>Use an ordinary USDB wallet address. No wallet connection or signature is required.</p>
        <form className={ styles.search } onSubmit={ event => void submit(event) }>
          <label>Receiving address<input value={ address } onChange={ event => setAddress(event.target.value) }
            readOnly={ Boolean(application) } placeholder="0x…" required spellCheck={ false } autoComplete="off" maxLength={ 42 }/></label>
          <button className={ styles.primary } type="submit" disabled={ busy || Boolean(claim) || (!application && status.status !== 'ready') }>
            { busy ? 'Submitting…' : application ? 'Retry same application' : 'Request test USDB' }
          </button>
        </form>
        { application && <p className={ styles.updated }>Application ID: c_{ application.id }</p> }
        { claim && <div className={ styles.notice } role="status">
          <p>{ progress[claim.status] || 'Checking transfer status' }</p>
          { /^0x[a-fA-F0-9]{64}$/.test(claim.transaction_hash || '') &&
            <a href={ '/tx/' + claim.transaction_hash }>View transfer in explorer</a> }
        </div> }
        { application && !busy && <button type="button" onClick={ reset }>Use another address</button> }
        { application && <p className={ styles.caption }>Starting another application does not cancel a transfer already queued.</p> }
      </div>
      <p className={ styles.notice }>Test USDB is for this test network only. This faucet does not provide Bitcoin or pay Bitcoin-side Miner Pass fees.</p>
      { /^0x[a-fA-F0-9]{40}$/.test(status.address || '') && <p>Faucet funding address: <a href={ '/address/' + status.address }>{ status.address }</a></p> }
    </> }
    { error && <ErrorNotice message={ error }/> }
  </section>;
}
