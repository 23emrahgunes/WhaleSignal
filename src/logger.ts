// Basit renkli/zaman damgali logger.
const ts = () => new Date().toISOString().slice(11, 23);

export const log = {
  info: (...a: unknown[]) => console.log(`[${ts()}] `, ...a),
  warn: (...a: unknown[]) => console.warn(`[${ts()}] ⚠ `, ...a),
  err: (...a: unknown[]) => console.error(`[${ts()}] ✖ `, ...a),
  trade: (...a: unknown[]) => console.log(`[${ts()}] 💱`, ...a),
  ok: (...a: unknown[]) => console.log(`[${ts()}] ✓ `, ...a),
};
