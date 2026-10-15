// From a frame leaving the server to it being heard: half the round trip, the
// jitter buffer and the output delay, each averaged since the last report.
// `stats` is an RTCStatsReport (or any Map of reports); `previous` is the
// `next` returned last time ({} at first).
export function playoutDelay(stats, previous) {
  const reports = [...stats.values()];
  const transport = reports.find(report => report.type === "transport");
  const pair = transport?.selectedCandidatePairId ? stats.get(transport.selectedCandidatePairId)
    : reports.find(report => report.type === "candidate-pair" && report.nominated);
  let delay = (pair?.currentRoundTripTime ?? 0) / 2;
  const next = {};
  for (const report of reports) {
    if (report.type === "inbound-rtp" && report.kind === "audio") {
      next.jitter = [report.jitterBufferDelay ?? 0, report.jitterBufferEmittedCount ?? 0];
    } else if (report.type === "media-playout") {
      next.playout = [report.totalPlayoutDelay ?? 0, report.totalSamplesCount ?? 0];
    }
  }
  for (const key of ["jitter", "playout"]) {
    const [total, count] = next[key] ?? [0, 0];
    const [lastTotal, lastCount] = previous[key] ?? [0, 0];
    if (count > lastCount) delay += (total - lastTotal) / (count - lastCount);
  }
  return {delay, next};
}
