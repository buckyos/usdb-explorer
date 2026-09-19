import React from 'react';

import type { State } from './api';

import styles from './usdb.module.css';

export function PageHeader({ title, description, children }: { title: string; description: string; children?: React.ReactNode }) {
  return <header className={ styles.header }>
    <div><p className={ styles.eyebrow }>USDB · TESTNET</p><h1>{ title }</h1><p>{ description }</p></div>
    <div className={ styles.actions }>{ children }</div>
  </header>;
}

export function ErrorNotice({ message }: { message: string }) {
  return <p className={ styles.error } role="alert">{ message }</p>;
}

export function StateReference({ state }: { state: State }) {
  return <details className={ styles.reference }>
    <summary>Bitcoin height { state.btc_height.toLocaleString('en-US') } · View data reference</summary>
    <dl><dt>Bitcoin block hash</dt><dd>{ state.stable_block_hash }</dd>
      <dt>Snapshot ID</dt><dd>{ state.snapshot_id }</dd>
      <dt>System state ID</dt><dd>{ state.system_state_id }</dd>
      <dt>Activation registry</dt><dd>{ state.activation_registry_id }</dd></dl>
  </details>;
}
