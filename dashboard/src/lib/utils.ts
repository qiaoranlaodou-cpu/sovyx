import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Exhaustive-check helper for discriminated-union switches.
 *
 * v0.31.7 T3.8 (LOW.8) — call from the ``default`` case of a switch
 * over a discriminated union; TypeScript will flag any future variant
 * added to the union but not handled in the switch as a compile-time
 * error (because the new variant won't be ``never`` at the call site).
 * The runtime ``throw`` is the safety net for the rare case where
 * the type lies (e.g. a server response that violates the schema —
 * runtime zod validation should catch it earlier, but this is the
 * last line of defense).
 */
export function assertNever(x: never): never {
  throw new Error(`Unexpected variant in exhaustive switch: ${JSON.stringify(x)}`);
}
