// Active EVM network for identity + (Phase 4) claim/tip transactions on Base.
// Base Sepolia under `npm run dev`, Base mainnet in production builds; override
// with VITE_NETWORK=mainnet | testnet.
import { base, baseSepolia } from 'viem/chains';

const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};
const isMainnet = (env.VITE_NETWORK ?? 'testnet') === 'mainnet';

export const NETWORK = {
  mainnet: isMainnet,
  testnet: !isMainnet,
  chain: isMainnet ? base : baseSepolia,
};
