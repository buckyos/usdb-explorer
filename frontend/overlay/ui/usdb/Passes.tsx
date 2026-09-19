import { useRouter } from 'next/router';
import React from 'react';

import type { PassDetail, PassPage, State } from './api';
import { amount, errorMessage, readPublic, short } from './api';
import { ErrorNotice, PageHeader, StateReference } from './shared';

import styles from './usdb.module.css';

const passID = /^[0-9a-f]{64}i(0|[1-9][0-9]{0,9})$/;
const scalar = (value: string | Array<string> | undefined) => typeof value === 'string' ? value : '';

export default function Passes() {
  const router = useRouter();
  const id = scalar(router.query.id);
  const height = scalar(router.query.height);
  const state = scalar(router.query.state);
  const cursor = scalar(router.query.cursor);
  const [ input, setInput ] = React.useState('');
  const [ atHeight, setAtHeight ] = React.useState('');
  const [ data, setData ] = React.useState<PassDetail | PassPage>();
  const [ error, setError ] = React.useState('');
  const [ busy, setBusy ] = React.useState(true);
  const [ tick, setTick ] = React.useState(0);

  React.useEffect(() => { setInput(id); setAtHeight(height); }, [ id, height ]);
  React.useEffect(() => {
    if (!router.isReady) { return; }
    let active = true;
    const controller = new AbortController();
    setData(undefined); setError(''); setBusy(true);
    const query = new URLSearchParams();
    if (height) { query.set('height', height); }
    if (state) { query.set('state', state); }
    if (cursor && !id) { query.set('cursor', cursor); }
    if ((id && !passID.test(id)) || (height && !/^(0|[1-9][0-9]*)$/.test(height))) {
      setError('Enter a complete inscription ID and a nonnegative Bitcoin height.'); setBusy(false);
      return () => { active = false; controller.abort(); };
    }
    readPublic<PassDetail | PassPage>('passes' + (id ? '/' + encodeURIComponent(id) : '') + '?' + query, controller.signal)
      .then(value => { if (active) { setData(value); } })
      .catch(error => { if (active && !controller.signal.aborted) { setError(error instanceof Error ? error.message : errorMessage()); } })
      .finally(() => { if (active) { setBusy(false); } });
    return () => { active = false; controller.abort(); };
  }, [ router.isReady, id, height, state, cursor, tick ]);

  const navigate = (query: Record<string, string>) => void router.push({ pathname: '/usdb/passes', query });
  const reference = (value: State) => ({ height: String(value.btc_height), state: value.system_state_id });
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const cleaned = input.trim().toLowerCase();
    if (cleaned && !passID.test(cleaned)) { setError('Enter an inscription ID: a 64-character transaction hash followed by i and the inscription index.'); return; }
    const query: Record<string, string> = {};
    if (cleaned) { query.id = cleaned; }
    if (atHeight.trim()) { query.height = atHeight.trim(); }
    navigate(query); setTick(tick + 1);
  };

  return <section className={ styles.root }>
    <PageHeader title="Miner Passes" description="Query Bitcoin inscriptions and their USDB mining state at a specific height.">
      <button onClick={ () => { navigate({}); setTick(tick + 1); } }>Latest active passes</button>
    </PageHeader>
    <form className={ styles.search } onSubmit={ submit }>
      <label>Inscription ID<input value={ input } onChange={ e => setInput(e.target.value) } placeholder="Transaction hash + i0 (leave blank to browse)" maxLength={ 80 } spellCheck={ false }/></label>
      <label className={ styles.height }>Bitcoin height<input value={ atHeight } onChange={ e => setAtHeight(e.target.value) } placeholder="Latest available" inputMode="numeric" maxLength={ 10 }/></label>
      <button type="submit" className={ styles.primary } disabled={ busy }>Search</button>
    </form>
    { error && <ErrorNotice message={ error }/> }
    { busy && <p role="status">Loading Miner Pass data…</p> }
    { error && <button onClick={ () => setTick(tick + 1) }>Retry</button> }
    { data && <>
      <StateReference state={ data.external_state }/>
      { 'items' in data ? <>
        <div className={ styles.sectionHeading }><h2>Active standard passes</h2><span>{ amount(data.total) } passes</span></div>
        <p className={ styles.caption }>Ordered by effective energy. This audit order does not predict the next block producer. Collaborative and inactive passes can be looked up by inscription ID.</p>
        { data.items.length === 0 ? <p className={ styles.empty }>No active standard passes at this Bitcoin height.</p> :
          <div className={ styles.tableScroll }><table><thead><tr><th>Inscription</th><th>State</th><th>Effective energy</th><th>Level</th><th>Difficulty factor</th></tr></thead>
            <tbody>{ data.items.map(pass => <tr key={ pass.pass_id }>
              <td><a href={ '/usdb/passes?' + new URLSearchParams({ id: pass.pass_id, ...reference(data.external_state) }) } title={ pass.pass_id }>{ short(pass.pass_id) }</a></td>
              <td><span className={ styles.badge }>{ pass.state }</span></td><td>{ amount(pass.effective_energy) }</td><td>{ pass.level }</td><td>{ pass.difficulty_factor_bps } bps</td>
            </tr>) }</tbody></table></div> }
        <div className={ styles.actions }>
          { cursor && <button onClick={ () => navigate(reference(data.external_state)) }>First page at this height</button> }
          { data.next_cursor && <button onClick={ () => navigate({ ...reference(data.external_state), cursor: data.next_cursor! }) }>Next page</button> }
        </div>
      </> : <>
        <div className={ styles.sectionHeading }><h2>Pass details</h2><span className={ styles.badge }>{ data.pass.state } · { data.pass.pass_kind }</span></div>
        <div className={ styles.panel }><dl>
          <dt>Inscription ID</dt><dd>{ data.pass.pass_id }</dd>
          <dt>Owner BTC address</dt><dd>{ data.pass.owner_btc_addr || 'Address mapping unavailable' }</dd>
          <dt>Owner script hash</dt><dd>{ data.pass.owner_script_hash }</dd>
          <dt>USDB reward address</dt><dd>{ data.pass.usdb_main ? <a href={ '/address/' + data.pass.usdb_main }>{ data.pass.usdb_main }</a> : 'Not applicable' }</dd>
          <dt>Minted at Bitcoin height</dt><dd>{ data.inscription.mint_block_height.toLocaleString('en-US') }</dd>
          <dt>Raw energy</dt><dd>{ amount(data.pass.raw_energy) }</dd>
          <dt>Collaboration contribution</dt><dd>{ amount(data.pass.collab_contribution) }</dd>
          <dt>Effective energy</dt><dd>{ amount(data.pass.effective_energy) }</dd>
          <dt>Level</dt><dd>{ data.pass.level }</dd>
          <dt>Difficulty factor</dt><dd>{ data.pass.difficulty_factor_bps } bps</dd>
          <dt>Contributing collab passes</dt><dd>{ data.pass.collab_breakdown_count }</dd>
          { data.inscription.leader_pass_id && <><dt>Declared Leader pass</dt><dd><a href={ '/usdb/passes?' + new URLSearchParams({ id: data.inscription.leader_pass_id, ...reference(data.external_state) }) }>{ data.inscription.leader_pass_id }</a></dd></> }
          { data.inscription.leader_btc_addr && <><dt>Declared Leader BTC address</dt><dd>{ data.inscription.leader_btc_addr }</dd></> }
          { Boolean(data.inscription.prev?.length) && <><dt>Previous passes</dt><dd>{ data.inscription.prev!.map(previous => <div key={ previous }><a href={ '/usdb/passes?' + new URLSearchParams({ id: previous, ...reference(data.external_state) }) }>{ previous }</a></div>) }</dd></> }
        </dl></div>
      </> }
      <p className={ styles.updated }>Retrieved { new Date(data.updated_at).toLocaleString() }. This view stays at the selected Bitcoin state; use “Latest active passes” for a new snapshot.</p>
    </> }
  </section>;
}
