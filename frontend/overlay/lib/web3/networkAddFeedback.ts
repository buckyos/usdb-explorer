type NetworkUrls = { rpcUrls: ReadonlyArray<string>; blockExplorerUrls?: ReadonlyArray<string> };

export const WALLET_REQUEST_PENDING = 'A wallet request is already pending. Open your wallet and approve or reject it before trying again.';
export const WALLET_NOT_FOUND = 'No compatible wallet was detected. Open this page in a browser with your wallet enabled, or use its network settings.';

/** Safe, Explorer-authored messages may pass through the upstream toast handler. */
class NetworkSetupError extends Error {}

/** Check advertised URLs without probing RPCs or changing the operator's addresses. */
export function networkSetupIssue(network: NetworkUrls): string | undefined {
  if (!Array.isArray(network.rpcUrls) || network.rpcUrls.length === 0) {
    return 'Public RPC settings are missing. Contact this Explorer\'s operator before adding the network.';
  }
  for (const [ field, urls ] of [ [ 'RPC', network.rpcUrls ], [ 'Explorer', network.blockExplorerUrls ?? [] ] ] as const) {
    if (!Array.isArray(urls)) {
      return 'The network URLs are invalid. Contact this Explorer\'s operator.';
    }
    for (const value of urls) {
      let url: URL;
      try {
        url = new URL(value);
      } catch {
        return 'The network URLs are invalid. Contact this Explorer\'s operator.';
      }
      // MetaMask's automatic-add exception covers localhost and 127.0.0.1,
      // not arbitrary LAN addresses, public testnets or a domain pointing to them.
      if (url.protocol === 'http:' && url.hostname !== 'localhost' && url.hostname !== '127.0.0.1') {
        return `Automatic network setup requires an HTTPS public ${ field } URL. ` +
          'Ask this Explorer\'s operator to enable HTTPS. HTTP browsing remains available.';
      }
      if (url.protocol !== 'https:' && url.protocol !== 'http:') {
        return 'The network URLs must use HTTPS (HTTP is allowed only for localhost development). Contact this Explorer\'s operator.';
      }
    }
  }
}

/** Preserve useful error categories without displaying raw wallet/RPC payloads. */
export function networkRequestError(error: unknown): string {
  if (error instanceof NetworkSetupError) {
    return error.message;
  }
  let current = error;
  // Some providers wrap the EIP-1193 error in data.originalError or cause.
  for (let depth = 0; depth < 4 && current && typeof current === 'object'; depth++) {
    const failure = current as { code?: unknown; data?: { originalError?: unknown }; cause?: unknown };
    switch (String(failure.code)) {
      case '4001': case 'ACTION_REJECTED':
        return 'Network request cancelled in your wallet. You can try again when ready.';
      case '-32002':
        return WALLET_REQUEST_PENDING;
      case '4200': case '-32601':
        return 'This wallet does not support automatic network setup. Use a compatible wallet, or add the network in its settings.';
      case '4100':
        return 'This site is not authorized in your wallet. Review its connection permissions and try again.';
      case '4900': case '4901':
        return 'Your wallet is disconnected from the network. Check its connection and the public RPC, then try again.';
      case '-32602':
        return 'Your wallet rejected the network settings. Check the public RPC URL, Chain ID and currency with this Explorer\'s operator.';
      default:
        current = failure.data?.originalError ?? failure.cause;
    }
  }
  return 'The wallet could not complete the network request. Open your wallet for details. If it keeps failing, contact this Explorer\'s operator.';
}

const pendingProviders = new WeakSet<object>();

/** Share validation and in-flight protection across both add-network entrypoints. */
export async function requestNetworkSetup(provider: object, network: NetworkUrls, request: () => Promise<unknown>): Promise<void> {
  const issue = networkSetupIssue(network);
  if (issue) {
    throw new NetworkSetupError(issue);
  }
  if (pendingProviders.has(provider)) {
    throw new NetworkSetupError(WALLET_REQUEST_PENDING);
  }
  pendingProviders.add(provider);
  try {
    await request();
  } finally {
    // Keep provider error codes intact for other upstream hook consumers;
    // the two UI entrypoints map them to safe messages at the display boundary.
    pendingProviders.delete(provider);
  }
}
