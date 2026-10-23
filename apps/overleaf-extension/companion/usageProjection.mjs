import { mergeUsageDetails } from "../shared/usageDetails.mjs";
const toNonNegativeNumber=value=>Math.max(0,Number(value)||0);
export function usageProjection(stats,slug) {
  const projectSlug = slug;
  const sessions = Array.isArray(stats.sessions) ? stats.sessions : [];

  const project = sessions.reduce(
    (acc, session) => {
      if (!session || session.project_slug !== projectSlug) return acc;
      const inputTokens = toNonNegativeNumber(session.input_tokens);
      const outputTokens = toNonNegativeNumber(session.output_tokens);
      acc.inputTokens += inputTokens;
      acc.outputTokens += outputTokens;
      acc.totalTokens += inputTokens + outputTokens;
      acc.costUsd += toNonNegativeNumber(session.cost_usd);
      acc.incompleteUsageRuns += toNonNegativeNumber(session.incomplete_usage_runs);
      acc.unconfirmedUsageRuns += toNonNegativeNumber(session.unconfirmed_usage_runs);
      acc.runCount += toNonNegativeNumber(session.run_count);
      return acc;
    },
    { inputTokens: 0, outputTokens: 0, totalTokens: 0, costUsd: 0, runCount: 0,
      incompleteUsageRuns: 0, unconfirmedUsageRuns: 0 }
  );
  const reportUsage = (stats.project_report_usage || []).find((entry) => entry.project_slug === projectSlug);
  if (reportUsage) {
    project.inputTokens += toNonNegativeNumber(reportUsage.input_tokens);
    project.outputTokens += toNonNegativeNumber(reportUsage.output_tokens);
    project.totalTokens += toNonNegativeNumber(reportUsage.input_tokens) + toNonNegativeNumber(reportUsage.output_tokens);
    project.costUsd += toNonNegativeNumber(reportUsage.cost_usd);
    project.runCount += toNonNegativeNumber(reportUsage.run_count);
  }
  const projectSessions = sessions.filter((s) => s?.project_slug === projectSlug);
  if (projectSessions.some((s) => s.usage_details) || reportUsage?.usage_details) {
    project.usageDetails = [...projectSessions, ...(reportUsage ? [reportUsage] : [])].reduce((d, s) =>
      mergeUsageDetails(d, s.usage_details || { cost_basis: "legacy_estimate" }), null);
  }
  project.costUsd = Number(project.costUsd.toFixed(6));
  project.accountingUnavailable = sessions.some((session) =>
    session?.project_slug === projectSlug && !Object.hasOwn(session, "incomplete_usage_runs"));

  const global = stats.global && typeof stats.global === "object" ? stats.global : {};
  const allInput = toNonNegativeNumber(global.input_tokens);
  const allOutput = toNonNegativeNumber(global.output_tokens);
  const allTime = {
    inputTokens: allInput,
    outputTokens: allOutput,
    totalTokens: toNonNegativeNumber(global.total_tokens) || allInput + allOutput,
    costUsd: Number(toNonNegativeNumber(global.cost_usd).toFixed(6)),
    ...(global.usage_details ? { usageDetails: global.usage_details } : {}),
    incompleteUsageRuns: toNonNegativeNumber(global.incomplete_usage_runs),
    unconfirmedUsageRuns: toNonNegativeNumber(global.unconfirmed_usage_runs),
    accountingUnavailable: !Object.hasOwn(global, "incomplete_usage_runs"),
    runCount: toNonNegativeNumber(global.session_count)
  };

  return { project, allTime };
}
