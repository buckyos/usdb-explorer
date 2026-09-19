export type State = {
  btc_height: number;
  snapshot_id: string;
  stable_block_hash: string;
  system_state_id: string;
  activation_registry_id: string;
};

export type Pass = {
  pass_id: string;
  owner_script_hash: string;
  owner_btc_addr?: string;
  usdb_main?: string;
  state: string;
  pass_kind: string;
  raw_energy: string;
  collab_contribution: string;
  effective_energy: string;
  level: number;
  difficulty_factor_bps: number;
  collab_breakdown_count: number;
};

export type PassPage = {
  updated_at: string;
  external_state: State;
  total: string;
  next_cursor: string | null;
  items: Array<Pass>;
};

export type PassDetail = {
  updated_at: string;
  external_state: State;
  pass: Pass;
  inscription: {
    mint_block_height: number;
    leader_pass_id: string | null;
    leader_btc_addr: string | null;
    prev: Array<string> | null;
  };
};

export type OverviewData = {
  updated_at: string;
  network: {
    name: string;
    chain_id: string;
    chain_id_hex: string;
    genesis_hash: string;
    bundle_id: string;
    btc_network: string;
    btc_index_origin_height: number;
    rpc_urls: Array<string>;
    explorer_urls: Array<string>;
    native_currency: { name: string; symbol: string; decimals: number };
  };
  chain: { height: string; hash: string; timestamp: string };
  explorer: { status: string; height?: string; hash?: string };
  indexer: { status: string; btc_height?: number; btc_stable_height?: number; error_code?: string };
};

const messages: Record<string, string> = {
  PASS_NOT_FOUND: 'This Miner Pass was not found at the selected Bitcoin height.',
  INDEXER_NOT_CONFIGURED: 'Miner Pass queries are not configured on this explorer.',
  INDEXER_NOT_READY: 'The Miner Pass indexer is catching up or recovering. Please try again later.',
  INDEXER_INCOMPATIBLE: 'The indexer does not provide the required Miner Pass query version.',
  INDEXER_NETWORK_MISMATCH: 'The indexer network does not match this explorer. Contact the operator.',
  STATE_CHANGED: 'The historical state changed. Start a new query to load a consistent view.',
  HISTORY_UNAVAILABLE: 'Data at this Bitcoin height is not available yet or is no longer retained.',
  INVALID_QUERY: 'Check the inscription ID and Bitcoin height, then start a new query.',
  RATE_LIMITED: 'Too many requests. Please wait a moment and try again.',
  CHAIN_IDENTITY_UNAVAILABLE: 'The USDB node is unavailable or its network identity could not be verified.',
};

export function errorMessage(code?: string): string {
  return messages[code || ''] || 'Data is temporarily unavailable. Please try again later.';
}

export async function readPublic<T>(resource: string, signal: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) { controller.abort(); }
  const timeout = setTimeout(abort, 20000);
  let response: Response;
  let value: unknown;
  try {
    response = await fetch('/api/usdb/v1/' + resource, { signal: controller.signal, credentials: 'omit', cache: 'no-store' });
    value = await response.json();
  } catch {
    throw new Error('Data could not be loaded. Check your connection and try again.');
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abort);
  }
  if (typeof value !== 'object' || value === null) {
    throw new Error('The explorer returned an incompatible response. Contact the operator.');
  }
  const envelope = value as { schema_version?: string; error?: { code?: string } };
  if (!response.ok) {
    throw new Error(errorMessage(envelope.error?.code));
  }
  if (envelope.schema_version !== 'usdb-explorer-public:v1') {
    throw new Error('The explorer returned an incompatible response. Contact the operator.');
  }
  return value as T;
}

export function short(value: string): string {
  return value.length > 24 ? value.slice(0, 12) + '…' + value.slice(-8) : value;
}

export function amount(value: string): string {
  // Energy must never pass through Number: UIP values can exceed 2^53.
  return /^\d+$/.test(value) ? BigInt(value).toLocaleString('en-US') : value;
}
