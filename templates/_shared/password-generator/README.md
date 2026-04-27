# `templates/_shared/password-generator/` — AS.0.10 TS twin

TypeScript twin of `backend/auth/password_generator.py`. Pure-functional,
side-effect free, suitable for emission into the generated-app workspace.

## Cross-twin contract

The Python and TypeScript sides MUST produce the same output distribution
for the same inputs, with the same wordlist, symbol pool, consonant /
vowel pools, and separator set. This is enforced by the drift-guard test
`backend/tests/test_password_generator.py::test_wordlist_parity_python_ts`
which:

1. Reads `index.ts` as text.
2. Regex-extracts the `DICEWARE_WORDLIST`, `SYMBOL_POOL`, `AMBIGUOUS_CHARS`,
   `CONSONANTS`, `VOWELS`, and `ALLOWED_SEPARATORS` constants.
3. Hashes each (newline-joined where applicable) with SHA-256.
4. Asserts equality with the Python `tuple` / `str` / `tuple` versions.

If you change one side, you MUST change the other. CI red is the canary.

## Why a TS twin and not just a JSON config?

OmniSight's productizer scaffolds new apps that bring along this lib at
build time. The TS surface lives next to other generated-app primitives
(`oauth-client/`, `token-vault/`, `bot-challenge/`, `honeypot/` per
the AS roadmap) so each generated app has password generation parity
with the OmniSight backend without runtime dependence on it.

## Public API

```ts
import {
  generateRandom,
  generateDiceware,
  generatePronounceable,
  generate,                         // dispatcher
  type GeneratedPassword,
  type PasswordStyle,
} from "./index"

const a = generateRandom({ length: 20 })
const b = generateDiceware({ numWords: 4, separator: "-", appendDigits: 2 })
const c = generatePronounceable({ numSyllables: 3, pairsPerSyllable: 3 })
const d = generate("random", { random: { length: 16 } })
```

All functions return `GeneratedPassword = { password, style, entropyBits, length }`.

## Randomness source

Uses `crypto.getRandomValues` (Web Crypto API) via rejection-sampled
`randBelow` for uniform integer picks. Throws if no Web Crypto is
available (e.g. legacy server runtime without `globalThis.crypto`).
