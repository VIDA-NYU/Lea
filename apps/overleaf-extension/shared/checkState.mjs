const LEAN_VALUES = new Set(["unformalized", "stubbed", "in-progress", "checked", "paused", "error"]);
const LEA_VALUES = new Set(["N/A", "in-progress", "paused", "warning", "error", "approved"]);

export function projectLeanCheck({ paneStatus = "", statusInfo = {} } = {}) {
  const status = String(paneStatus || "").toLowerCase();
  let value = "unformalized";
  if (status === "stub-generated") value = "stubbed";
  else if (status === "in-progress") value = "in-progress";
  else if (["valid", "defined", "disproved", "stale"].includes(status)) value = "checked";
  else if (status === "needs-review" && statusInfo?.leanCheckPassed === true) value = "checked";
  else if (status === "paused") value = "paused";
  else if (["invalid", "error", "unknown"].includes(status)) value = "error";
  return {
    status: value,
    stopReason: statusInfo?.stopReason || null,
    recoverable: value === "paused" || statusInfo?.recoverable === true,
    detail: statusInfo?.message || statusInfo?.leanCheck?.message || null
  };
}

export function normalizeLeaCheck(value) {
  const check = value && typeof value === "object" ? value : {};
  const status = LEA_VALUES.has(check.status) ? check.status : "N/A";
  return {
    ...check,
    status,
    reason: check.reason || (status === "N/A" ? "not_evaluated" : null),
    current: check.current !== false
  };
}

export function isDualCheckStatus(value) {
  return LEAN_VALUES.has(value?.leanCheck?.status) && LEA_VALUES.has(value?.leaCheck?.status);
}
