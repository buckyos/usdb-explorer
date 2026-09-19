import { useRouter } from 'next/router';
import React from 'react';

import { amount, readPublic, short } from './api';
import { ErrorNotice, PageHeader } from './shared';

import styles from './usdb.module.css';

type EconomicsReport = {
  status: 'verified' | 'genesis_not_applicable';
  block_hash: string; block_number: string; parent_hash: string; state_root: string; receipts_root: string;
  miner: string; dividend: string; versions?: Record<string, number>;
  selector?: { pass_id: string; btc_height: number; btc_anchor_age_blocks: number; snapshot_id: string; system_state_id: string };
  amounts?: { issued_before_atoms: string; issued_after_atoms: string; emission_atoms: string; miner_emission_atoms: string;
    fees_atoms: string; miner_fees_atoms: string; dao_fees_atoms: string };
  transactions: Array<{ hash: string; status: number; gas_used: string; effective_gas_price_atoms: string;
    fee_atoms: string; miner_fee_atoms: string; dao_fee_atoms: string; fee_route: string }>;
};

// USDB uses 18 decimals. Keep both the integer and fractional parts exact.
function coins(atoms: string): string {
  const padded = atoms.padStart(19, '0');
  const fraction = padded.slice(-18).replace(/0+$/, '');
  return amount(padded.slice(0, -18)) + (fraction ? '.' + fraction : '');
}
function Money({ value }: { value: string }) {
  return <span title={ value + ' atoms' }>{ coins(value) } USDB</span>;
}

export default function Economics() {
  const router = useRouter();
  const selected = typeof router.query.block === 'string' ? router.query.block : '';
  const [ input, setInput ] = React.useState(selected);
  const [ data, setData ] = React.useState<EconomicsReport>();
  const [ busy, setBusy ] = React.useState(false);
  const [ error, setError ] = React.useState('');
  const [ tick, setTick ] = React.useState(0);

  React.useEffect(() => {
    setInput(selected); setData(undefined); setError(''); setBusy(false);
    if (!selected) { return; }
    if (!/^(0|[1-9][0-9]{0,19}|0x[0-9a-f]{64})$/.test(selected) ||
      (!selected.startsWith('0x') && BigInt(selected) > BigInt('18446744073709551615'))) {
      setError('Enter a USDB block number or a full block hash.'); return;
    }
    const controller = new AbortController();
    setBusy(true);
    void readPublic<{ economics: EconomicsReport }>('blocks/' + selected + '/economics', controller.signal)
      .then(value => { if (!controller.signal.aborted) { setData(value.economics); } })
      .catch(error => { if (!controller.signal.aborted) { setError(error.message); } })
      .finally(() => { if (!controller.signal.aborted) { setBusy(false); } });
    return () => controller.abort();
  }, [ selected, tick ]);

  const search = (event: React.FormEvent) => {
    event.preventDefault();
    const block = input.trim().toLowerCase();
    if (block === selected) { setTick(tick + 1); }
    else { void router.push({ pathname: '/usdb/economics', query: { block } }, undefined, { shallow: true }); }
  };
  const a = data?.amounts;
  const s = data?.selector;
  return <section className={ styles.root }>
    <PageHeader title="Block economics" description="Trace a USDB block from its Miner Pass to emission and transaction fees."/>
    <form className={ styles.search } onSubmit={ search }>
      <label>USDB block number or hash<input value={ input } onChange={ event => setInput(event.target.value) } required spellCheck={ false }/></label>
      <button className={ styles.primary } disabled={ busy } type="submit">Verify block</button>
    </form>
    { !selected && <p className={ styles.notice }>Choose a block to verify. You can also open this page from any block's details.</p> }
    { busy && <p role="status">Replaying and verifying this block…</p> }
    { error && <><ErrorNotice message={ error }/><button onClick={ () => setTick(tick + 1) }>Retry</button></> }
    { data && <>
      <div className={ styles.panel }><h2>Block { amount(data.block_number) }</h2>
        <dl><dt>Block hash</dt><dd><a href={ '/block/' + data.block_hash }>{ data.block_hash }</a></dd>
          { data.status === 'verified' && <><dt>Reward address</dt><dd><a href={ '/address/' + data.miner }>{ data.miner }</a></dd></> }</dl>
      </div>
      { data.status === 'genesis_not_applicable' && <p className={ styles.notice } role="status">Genesis has no mined block reward or transaction fees. Genesis allocations are not shown as mining emission.</p> }
      { data.status === 'verified' && a && s && <>
        <p className={ styles.notice } role="status">Verified for this block: execution matches the committed state root, receipts, bloom and gas usage.
          This is a check by this node, not an independent network audit. Canonical status was checked when loaded.</p>
        <div className={ `${ styles.cards } ${ styles.economicCards }` }>
          <article><h2>New emission</h2><strong><Money value={ a.emission_atoms }/></strong><p>Newly issued in this block</p></article>
          <article><h2>Transaction fees</h2><strong><Money value={ a.fees_atoms }/></strong><p>After gas refunds, including reverted calls</p></article>
          <article><h2>Miner fee income</h2><strong><Money value={ a.miner_fees_atoms }/></strong><p>Separate from new emission</p></article>
          <article><h2>Dividend fee income</h2><strong><Money value={ a.dao_fees_atoms }/></strong><p>According to this block's active fee gate</p></article>
        </div>
        <div className={ styles.panel }><h2>Miner Pass and issuance</h2>
          <dl><dt>Selected Miner Pass</dt><dd><a href={ '/usdb/passes?id=' + s.pass_id + '&height=' + s.btc_height + '&state=' + s.system_state_id }>{ s.pass_id }</a></dd>
            <dt>Bitcoin anchor height</dt><dd>{ s.btc_height.toLocaleString('en-US') }</dd>
            <dt>Anchor age in USDB blocks</dt><dd>{ s.btc_anchor_age_blocks }</dd>
            <dt>Miner emission credit</dt><dd><Money value={ a.miner_emission_atoms }/></dd>
            <dt>Dividend address</dt><dd>{ /^0x0{40}$/.test(data.dividend) ? 'Not configured' : <a href={ '/address/' + data.dividend }>{ data.dividend }</a> }</dd>
            <dt>Issued counter before</dt><dd><Money value={ a.issued_before_atoms }/></dd>
            <dt>Issued counter after</dt><dd><Money value={ a.issued_after_atoms }/></dd></dl>
          <p className={ styles.notice }>The issued counter includes genesis allocations and protocol emission. It is not circulating supply.
            Fees redistribute existing USDB. These credits are not the recipient's net balance change.</p>
        </div>
        <details className={ styles.reference }><summary>Verification references and rule versions</summary>
          <dl><dt>Parent hash</dt><dd>{ data.parent_hash }</dd><dt>State root</dt><dd>{ data.state_root }</dd>
            <dt>Receipts root</dt><dd>{ data.receipts_root }</dd><dt>Snapshot ID</dt><dd>{ s.snapshot_id }</dd>
            <dt>System state ID</dt><dd>{ s.system_state_id }</dd>
            { Object.entries(data.versions || {}).map(([ key, value ]) => <React.Fragment key={ key }><dt>{ key }</dt><dd>{ value }</dd></React.Fragment>) }</dl>
        </details>
        <h2>Transaction fee breakdown</h2>
        { data.transactions.length === 0 ? <p className={ styles.empty }>This block has no transactions. Transaction fees are zero.</p> :
          <div className={ styles.tableScroll }><table><thead><tr><th>Transaction</th><th>Result</th><th>Gas used</th><th>Paid fee</th><th>Miner</th><th>Dividend</th><th>Fee route</th></tr></thead>
            <tbody>{ data.transactions.map(tx => <tr key={ tx.hash }><td><a href={ '/tx/' + tx.hash }>{ short(tx.hash) }</a></td>
              <td>{ tx.status === 1 ? 'Success' : 'Reverted' }</td><td>{ amount(tx.gas_used) }</td><td><Money value={ tx.fee_atoms }/></td>
              <td><Money value={ tx.miner_fee_atoms }/></td><td><Money value={ tx.dao_fee_atoms }/></td>
              <td>{ tx.fee_route === 'miner_only' ? 'Miner only' : 'Miner + Dividend' }</td></tr>) }</tbody></table></div> }
      </> }
    </> }
  </section>;
}
