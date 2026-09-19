import React from 'react';

import type { OverviewData } from './api';
import { amount, errorMessage, readPublic } from './api';
import { ErrorNotice, PageHeader } from './shared';

import styles from './usdb.module.css';

type Wallet = { request: (request: { method: string; params: Array<unknown> }) => Promise<unknown> };

export default function Overview() {
  const [ data, setData ] = React.useState<OverviewData>();
  const [ error, setError ] = React.useState('');
  const [ busy, setBusy ] = React.useState(true);
  const [ tick, setTick ] = React.useState(0);
  const [ walletMessage, setWalletMessage ] = React.useState('');

  React.useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const update = async() => {
      if (!active) { return; }
      setBusy(true);
      try {
        const value = await readPublic<OverviewData>('overview', controller.signal);
        if (active) { setData(value); setError(''); }
      } catch (error) {
        if (active && !controller.signal.aborted) { setError(error instanceof Error ? error.message : errorMessage()); }
      } finally {
        if (active) {
          setBusy(false);
          timer = setTimeout(() => { void update(); }, 15000);
        }
      }
    };
    void update();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [ tick ]);

  const addNetwork = async() => {
    const wallet = (window as unknown as { ethereum?: Wallet }).ethereum;
    if (!wallet || !data) { setWalletMessage('Open this page in a compatible wallet, or use the network details below.'); return; }
    try {
      await wallet.request({ method: 'wallet_addEthereumChain', params: [ {
        chainId: data.network.chain_id_hex, chainName: data.network.name,
        nativeCurrency: data.network.native_currency, rpcUrls: data.network.rpc_urls,
        blockExplorerUrls: data.network.explorer_urls,
      } ] });
      setWalletMessage('Network details sent to your wallet.');
    } catch { setWalletMessage('Your wallet did not add this network. You can enter the details below manually.'); }
  };

  const lag = data?.explorer.height !== undefined && BigInt(data.chain.height) >= BigInt(data.explorer.height)
    ? (BigInt(data.chain.height) - BigInt(data.explorer.height)).toString() : null;

  return <section className={ styles.root }>
    <PageHeader title="Network overview" description="USDB chain and Bitcoin-side indexing, shown separately.">
      <button onClick={ () => setTick(tick + 1) } disabled={ busy }>Refresh</button>
      <button className={ styles.primary } onClick={ () => void addNetwork() } disabled={ !data }>Add network to wallet</button>
    </PageHeader>
    { walletMessage && <p role="status" className={ styles.notice }>{ walletMessage }</p> }
    { error && <ErrorNotice message={ data ? error + ' Previously loaded values are shown below.' : error }/> }
    { !data && busy && <p role="status">Loading network data…</p> }
    { data && <>
      <p className={ styles.updated } role="status">Updated { new Date(data.updated_at).toLocaleString() } · Refreshes every 15 seconds{ busy ? ' · Refreshing…' : '' }</p>
      <div className={ styles.cards }>
        <article><h2>USDB chain head</h2><strong>{ amount(data.chain.height) }</strong><p>Latest block reported by this node</p></article>
        <article><h2>Explorer indexed height</h2><strong>{ data.explorer.height === undefined ? 'Unavailable' : amount(data.explorer.height) }</strong>
          <p>{ lag === null ? 'Index status is not available' : `${ amount(lag) } blocks behind the observed chain head` }</p></article>
        <article><h2>Miner Pass index</h2><strong>{ data.indexer.btc_height?.toLocaleString('en-US') ?? 'Unavailable' }</strong><p>Bitcoin height · { data.indexer.status }</p></article>
        <article><h2>Bitcoin stable target</h2><strong>{ data.indexer.btc_stable_height?.toLocaleString('en-US') ?? 'Unavailable' }</strong><p>{ data.network.btc_network }</p></article>
      </div>
      { data.indexer.error_code && <ErrorNotice message={ errorMessage(data.indexer.error_code) }/> }
      <div className={ styles.panel }><h2>Connect to { data.network.name }</h2>
        <dl><dt>Chain ID</dt><dd>{ data.network.chain_id } ({ data.network.chain_id_hex })</dd>
          <dt>Currency</dt><dd>{ data.network.native_currency.symbol } · { data.network.native_currency.decimals } decimals</dd>
          <dt>Public RPC</dt><dd>{ data.network.rpc_urls.join(', ') }</dd>
          <dt>Explorer</dt><dd>{ data.network.explorer_urls.join(', ') }</dd>
          <dt>Genesis hash</dt><dd>{ data.network.genesis_hash }</dd>
          <dt>Chain head hash</dt><dd><a href={ '/block/' + data.chain.hash }>{ data.chain.hash }</a></dd>
          <dt>Latest block time</dt><dd>{ new Date(Number(data.chain.timestamp) * 1000).toLocaleString() }</dd>
          <dt>Bitcoin indexing starts at</dt><dd>{ data.network.btc_index_origin_height.toLocaleString('en-US') }</dd></dl>
      </div>
      <p className={ styles.notice }>These heights describe this explorer and its data sources. They do not certify network-wide synchronization.
        Miner Passes use Bitcoin { data.network.btc_network === 'btc-mainnet' ? 'mainnet' : 'testnet' }; USDB test coins cannot pay Bitcoin inscription fees.</p>
      <a className={ styles.cta } href="/usdb/passes">Explore Miner Passes →</a>
    </> }
  </section>;
}
