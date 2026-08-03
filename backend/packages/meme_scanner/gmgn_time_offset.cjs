const offsetMs = Number.parseInt(process.env.GMGN_TIME_OFFSET_MS || "0", 10);

if (Number.isFinite(offsetMs) && offsetMs !== 0) {
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + offsetMs;
}
