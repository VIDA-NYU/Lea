import { useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft,
  Cpu,
  DollarSign,
  Hash,
  Loader2,
  MessageSquare,
  TrendingUp,
  Zap,
  Clock,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  SessionDetail,
  UsageBreakdownRow,
  UsageSessionSummary,
  UsageStats,
  getSession,
  getUsageStats,
} from '../lib/api';
import { OriginBadge } from './OriginBadge';

// Monochrome-first Lea palette; extra hues are reserved for chart series where
// color distinguishes data rather than decorating product chrome.
const INPUT_COLOR = '#1c1c20'; // --accent
const OUTPUT_COLOR = '#2563a8'; // --fn
const MONEY_COLOR = '#2f8f5b'; // --green
const MODEL_COLORS = ['#1c1c20', '#2563a8', '#2f8f5b', '#c0872e', '#7c3aed', '#d14343'];
const LIVE_STATS_REFRESH_MS = 1000;

function fmtNumber(value: number, digits = 0) {
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtCost(value: number, digits = 4) {
  if (value > 0 && value < 0.0001) {
    return '<$0.0001';
  }
  return `$${value.toFixed(digits)}`;
}

function fmtDate(iso: string | null | undefined) {
  if (!iso) {
    return 'Unknown date';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'Unknown date';
  }
  return date.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

function fmtDayLabel(day: string) {
  const date = new Date(`${day}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return day;
  }
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtDuration(seconds: number) {
  if (seconds < 60) {
    return `${Math.max(0, seconds)}s`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function modelColor(model: string | null | undefined) {
  if (!model) {
    return '#8b8b93'; // --muted
  }
  let hash = 0;
  for (const char of model) {
    hash = (hash + char.charCodeAt(0)) % MODEL_COLORS.length;
  }
  return MODEL_COLORS[hash];
}

function modelLabel(model: string | null | undefined) {
  if (!model) {
    return 'No model recorded';
  }
  return model
    .replace(/^anthropic\//, '')
    .replace(/^openai\//, '')
    .replace(/^google\//, '')
    .replace(/-/g, ' ');
}

function originLabel(origin: string) {
  if (origin === 'ui') return 'Direct (UI)';
  if (origin === 'overleaf') return 'Overleaf extension';
  return origin || 'Unknown';
}

function originColor(origin: string) {
  if (origin === 'overleaf') return INPUT_COLOR; // terracotta accent
  if (origin === 'ui') return OUTPUT_COLOR; // blue
  return '#8b8b93'; // --muted
}

function groupSessions(sessions: UsageSessionSummary[]) {
  const groups = new Map<string, UsageSessionSummary[]>();
  for (const session of sessions) {
    const key = (session.started_at || session.created_at || '').slice(0, 10) || 'unknown';
    groups.set(key, [...(groups.get(key) || []), session]);
  }
  return [...groups.entries()].sort(([a], [b]) => b.localeCompare(a));
}

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
  accent,
}: {
  icon: typeof Zap;
  label: string;
  value: string;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-2 text-muted-foreground">
        <Icon className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate text-[0.65rem] uppercase tracking-widest">{label}</span>
      </div>
      <span className="break-words font-mono text-2xl leading-tight" style={{ color: accent }}>
        {value}
      </span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  );
}

function SessionListPane({
  sessions,
  selectedId,
  onSelect,
}: {
  sessions: UsageSessionSummary[];
  selectedId?: string;
  onSelect: (id: string) => void;
}) {
  const groups = groupSessions(sessions);

  return (
    <aside className="flex min-h-0 flex-col border-r border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <MessageSquare className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          Sessions
        </span>
        <span className="ml-auto rounded bg-muted px-1.5 font-mono text-[0.65rem] text-muted-foreground">
          {sessions.length}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-2">
        {sessions.length === 0 ? (
          <div className="px-4 py-6 text-sm text-muted-foreground">No sessions recorded yet.</div>
        ) : (
          groups.map(([day, daySessions]) => (
            <div key={day}>
              <div className="sticky top-0 bg-background px-4 py-1.5 text-[0.6rem] uppercase tracking-widest text-muted-foreground">
                {day === 'unknown' ? 'Unknown date' : fmtDayLabel(day)}
              </div>
              {daySessions.map((session) => {
                const isSelected = session.id === selectedId;
                const color = modelColor(session.primary_model);
                return (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => onSelect(session.id)}
                    className={[
                      'flex w-full flex-col gap-1 px-4 py-2.5 text-left transition-colors',
                      isSelected ? 'bg-accent' : 'hover:bg-accent/50',
                    ].join(' ')}
                  >
                    <span
                      className={[
                        'truncate text-sm',
                        isSelected ? 'text-foreground' : 'text-muted-foreground',
                      ].join(' ')}
                    >
                      {session.title}
                    </span>
                    <span className="flex min-w-0 items-center gap-2 font-mono text-[0.65rem]">
                      <span className="truncate" style={{ color }}>
                        {modelLabel(session.primary_model)}
                      </span>
                      <span className="shrink-0 text-muted-foreground">
                        {fmtCost(session.cost_usd)}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          ))
        )}
      </div>
    </aside>
  );
}

type StatsSessionDetail = UsageSessionSummary & { usage_breakdown?: UsageBreakdownRow[] };

function SessionDetailPane({ session }: { session?: StatsSessionDetail }) {
  if (!session) {
    return (
      <section className="flex min-h-0 flex-col border-r border-border">
        <PaneHeader icon={Hash} label="Session Detail" />
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
          Select a session to inspect usage.
        </div>
      </section>
    );
  }

  const totalTokens = session.total_tokens;
  const inputPct = totalTokens ? Math.round((session.input_tokens / totalTokens) * 100) : 0;
  const tokensPerMessage = session.message_count ? Math.round(totalTokens / session.message_count) : 0;
  const tokensPerMinute =
    session.duration_seconds > 0 ? Math.round(totalTokens / (session.duration_seconds / 60)) : 0;
  const costPerThousand = totalTokens ? session.cost_usd / (totalTokens / 1000) : 0;
  const color = modelColor(session.primary_model);

  return (
    <section className="flex min-h-0 flex-col border-r border-border">
      <PaneHeader icon={Hash} label="Session Detail" />
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="flex flex-col gap-6">
          <div className="flex flex-col gap-2">
            <h2 className="text-2xl leading-snug text-foreground">{session.title}</h2>
            <div className="flex flex-wrap items-center gap-3">
              <span
                className="rounded border px-2 py-0.5 font-mono text-[0.7rem]"
                style={{ borderColor: `${color}55`, background: `${color}18`, color }}
              >
                {modelLabel(session.primary_model)}
              </span>
              <OriginBadge origin={session.origin} originUrl={session.origin_url} showDirect />
              <span className="text-xs text-muted-foreground">{fmtDate(session.started_at)}</span>
              <span className="text-xs text-muted-foreground">{fmtDuration(session.duration_seconds)}</span>
              <span className="text-xs text-muted-foreground">{session.run_count} runs</span>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <StatCard
              icon={Zap}
              label="Total tokens"
              value={fmtNumber(totalTokens)}
              sub={`${fmtNumber(session.input_tokens)} in - ${fmtNumber(session.output_tokens)} out`}
            />
            <StatCard
              icon={DollarSign}
              label="Session cost"
              value={fmtCost(session.cost_usd)}
              sub={totalTokens ? `${fmtCost(costPerThousand)} / 1K tokens` : 'No token usage recorded'}
              accent={MONEY_COLOR}
            />
            <StatCard
              icon={MessageSquare}
              label="Messages"
              value={fmtNumber(session.message_count)}
              sub={tokensPerMessage ? `${fmtNumber(tokensPerMessage)} tokens / msg` : 'No messages recorded'}
            />
            <StatCard
              icon={Clock}
              label="Duration"
              value={fmtDuration(session.duration_seconds)}
              sub={tokensPerMinute ? `${fmtNumber(tokensPerMinute)} tokens / min` : 'No duration rate yet'}
            />
          </div>

          <TokenSplit
            label="Token split"
            inputTokens={session.input_tokens}
            outputTokens={session.output_tokens}
          />

          <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
            <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
              Cost summary
            </span>
            <Row label="Input tokens" value={`${fmtNumber(session.input_tokens)} tok`} />
            <Row label="Output tokens" value={`${fmtNumber(session.output_tokens)} tok`} />
            <Row label="Model runs" value={fmtNumber(session.run_count)} />
            <div className="border-t border-border pt-2">
              <Row label="Recorded total" value={fmtCost(session.cost_usd)} accent={MONEY_COLOR} />
            </div>
          </div>

          <TurnCostBreakdown rows={session.usage_breakdown || []} runCount={session.run_count} />
        </div>
      </div>
    </section>
  );
}

function TurnCostBreakdown({
  rows,
  runCount,
}: {
  rows: UsageBreakdownRow[];
  runCount: number;
}) {
  const rowsByRun = rows.reduce((groups, row) => {
    const key = row.run_number || 1;
    groups.set(key, [...(groups.get(key) || []), row]);
    return groups;
  }, new Map<number, UsageBreakdownRow[]>());
  const groupedRows = [...rowsByRun.entries()].sort(([a], [b]) => a - b);

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          Turn cost breakdown
        </span>
        {rows.length > 0 && (
          <span className="font-mono text-[0.65rem] text-muted-foreground">
            {rows.length} rows
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        <div className="rounded-md bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          No turn-level cost events recorded for this session.
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-border">
          {groupedRows.map(([runNumber, runRows], groupIndex) => {
            const runInputTokens = runRows.reduce((total, row) => total + row.input_tokens, 0);
            const runOutputTokens = runRows.reduce((total, row) => total + row.output_tokens, 0);
            const runCost = runRows.reduce((total, row) => total + row.cost_usd, 0);
            return (
              <div key={runNumber} className={groupIndex > 0 ? 'border-t border-border' : ''}>
                {runCount > 1 && (
                  <div className="flex items-center justify-between gap-3 bg-muted/60 px-3 py-2">
                    <span className="font-mono text-[0.65rem] uppercase tracking-widest text-foreground">
                      Run {runNumber}
                    </span>
                    <span className="shrink-0 font-mono text-[0.65rem] text-muted-foreground">
                      {fmtNumber(runInputTokens + runOutputTokens)} tok - {fmtCost(runCost)}
                    </span>
                  </div>
                )}
                <div className="grid grid-cols-[minmax(0,1fr)_5rem_5rem_5rem] gap-2 border-b border-border bg-muted/40 px-3 py-2 font-mono text-[0.6rem] uppercase tracking-widest text-muted-foreground">
                  <span>Step</span>
                  <span className="text-right">In</span>
                  <span className="text-right">Out</span>
                  <span className="text-right">Cost</span>
                </div>
                <div className="divide-y divide-border">
                  {runRows.map((row) => (
                    <div
                      key={`${row.run_id || row.run_number}-${row.ordinal}-${row.phase}`}
                      className="grid grid-cols-[minmax(0,1fr)_5rem_5rem_5rem] gap-2 px-3 py-2"
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm text-foreground">{row.label}</div>
                        <div className="font-mono text-[0.65rem] text-muted-foreground">
                          {fmtNumber(row.total_tokens)} tok
                        </div>
                      </div>
                      <span className="self-center text-right font-mono text-xs text-muted-foreground">
                        {fmtNumber(row.input_tokens)}
                      </span>
                      <span className="self-center text-right font-mono text-xs text-muted-foreground">
                        {fmtNumber(row.output_tokens)}
                      </span>
                      <span className="self-center text-right font-mono text-xs" style={{ color: MONEY_COLOR }}>
                        {fmtCost(row.cost_usd)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          </div>
      )}
    </div>
  );
}

function GlobalStatsPane({ stats }: { stats?: UsageStats }) {
  const dailyData = useMemo(
    () =>
      (stats?.daily || []).map((point) => ({
        ...point,
        label: fmtDayLabel(point.day),
      })),
    [stats?.daily],
  );

  if (!stats) {
    return (
      <section className="flex min-h-0 flex-col">
        <PaneHeader icon={TrendingUp} label="Global Statistics" />
        <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
          No statistics loaded.
        </div>
      </section>
    );
  }

  const global = stats.global;
  const inputPct = global.total_tokens
    ? Math.round((global.input_tokens / global.total_tokens) * 100)
    : 0;

  return (
    <section className="flex min-h-0 flex-col">
      <PaneHeader icon={TrendingUp} label="Global Statistics" />
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <StatCard
              icon={DollarSign}
              label="Total spent"
              value={`$${global.cost_usd.toFixed(2)}`}
              accent={MONEY_COLOR}
            />
            <StatCard icon={Zap} label="Total tokens" value={fmtNumber(global.total_tokens)} />
            <StatCard icon={MessageSquare} label="Sessions" value={fmtNumber(global.session_count)} />
            <StatCard icon={Hash} label="Messages" value={fmtNumber(global.message_count)} />
          </div>

          <ChartBlock title="Daily token usage">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={dailyData} margin={{ top: 4, right: 0, bottom: 0, left: -28 }}>
                <defs>
                  <linearGradient id="tokenGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={INPUT_COLOR} stopOpacity={0.3} />
                    <stop offset="95%" stopColor={INPUT_COLOR} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="label" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                <Tooltip formatter={(value: number) => [fmtNumber(value), 'tokens']} />
                <Area
                  type="monotone"
                  dataKey="total_tokens"
                  stroke={INPUT_COLOR}
                  strokeWidth={1.5}
                  fill="url(#tokenGrad)"
                  dot={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </ChartBlock>

          <ChartBlock title="Daily cost">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={dailyData} margin={{ top: 4, right: 0, bottom: 0, left: -28 }}>
                <XAxis dataKey="label" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                <Tooltip formatter={(value: number) => [fmtCost(value, 2), 'cost']} />
                <Bar dataKey="cost_usd" fill={MONEY_COLOR} radius={[2, 2, 0, 0]} maxBarSize={18} />
              </BarChart>
            </ResponsiveContainer>
          </ChartBlock>

          <TokenSplit
            label="All-time token split"
            inputTokens={global.input_tokens}
            outputTokens={global.output_tokens}
          />

          <div className="flex flex-col gap-2">
            <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
              By model
            </span>
            {stats.models.length === 0 ? (
              <div className="text-sm text-muted-foreground">No model usage recorded yet.</div>
            ) : (
              stats.models.map((row) => {
                const pct = global.cost_usd ? Math.round((row.cost_usd / global.cost_usd) * 100) : inputPct;
                const color = modelColor(row.model);
                return (
                  <div key={row.model} className="flex flex-col gap-1">
                    <div className="flex min-w-0 items-center justify-between gap-3">
                      <span className="truncate font-mono text-xs" style={{ color }}>
                        {modelLabel(row.model)}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">
                        {row.session_count} sess - {fmtNumber(row.total_tokens)} tok - {fmtCost(row.cost_usd, 2)}
                      </span>
                    </div>
                    <div className="h-1 rounded-full bg-muted">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.max(2, pct)}%`, background: color }}
                      />
                    </div>
                  </div>
                );
              })
            )}
          </div>

          {/* Direct (UI) vs Overleaf-extension usage, from store.usage_stats().origins
              (aggregated from the same sessions the global totals use). */}
          <div className="flex flex-col gap-2">
            <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
              By origin
            </span>
            {(stats.origins ?? []).every((row) => row.session_count === 0) ? (
              <div className="text-sm text-muted-foreground">No sessions recorded yet.</div>
            ) : (
              (stats.origins ?? []).map((row) => {
                const pct = global.cost_usd
                  ? Math.round((row.cost_usd / global.cost_usd) * 100)
                  : global.session_count
                  ? Math.round((row.session_count / global.session_count) * 100)
                  : 0;
                const color = originColor(row.origin);
                return (
                  <div key={row.origin} className="flex flex-col gap-1">
                    <div className="flex min-w-0 items-center justify-between gap-3">
                      <span className="truncate text-xs" style={{ color }}>
                        {originLabel(row.origin)}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">
                        {row.session_count} sess - {fmtNumber(row.total_tokens)} tok - {fmtCost(row.cost_usd, 2)}
                      </span>
                    </div>
                    <div className="h-1 rounded-full bg-muted">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.max(2, pct)}%`, background: color }}
                      />
                    </div>
                  </div>
                );
              })
            )}
          </div>

          <div className="flex flex-col gap-2 rounded-lg border border-border p-4">
            <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
              Averages per session
            </span>
            <Row label="Tokens" value={fmtNumber(global.average_tokens_per_session)} />
            <Row label="Cost" value={fmtCost(global.average_cost_per_session)} />
            <Row label="Messages" value={fmtNumber(global.average_messages_per_session)} />
          </div>
        </div>
      </div>
    </section>
  );
}

function PaneHeader({ icon: Icon, label }: { icon: typeof Hash; label: string }) {
  return (
    <div className="flex items-center gap-2 border-b border-border px-6 py-3">
      <Icon className="h-3.5 w-3.5 text-muted-foreground" />
      <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">{label}</span>
    </div>
  );
}

function TokenSplit({
  label,
  inputTokens,
  outputTokens,
}: {
  label: string;
  inputTokens: number;
  outputTokens: number;
}) {
  const total = inputTokens + outputTokens;
  const inputPct = total ? Math.round((inputTokens / total) * 100) : 0;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-[0.7rem] text-muted-foreground">
          {inputPct}% input - {100 - inputPct}% output
        </span>
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-muted">
        <div className="h-full" style={{ width: `${inputPct}%`, background: INPUT_COLOR }} />
        <div className="h-full flex-1" style={{ background: OUTPUT_COLOR }} />
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: INPUT_COLOR }} />
          Input {fmtNumber(inputTokens)}
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: OUTPUT_COLOR }} />
          Output {fmtNumber(outputTokens)}
        </span>
      </div>
    </div>
  );
}

function Row({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-right font-mono text-sm" style={{ color: accent }}>
        {value}
      </span>
    </div>
  );
}

function ChartBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
        {title}
      </span>
      <div className="h-28">{children}</div>
    </div>
  );
}

export function StatsPage({ onBack }: { onBack: () => void }) {
  const [stats, setStats] = useState<UsageStats>();
  const [sessionDetails, setSessionDetails] = useState<Record<string, SessionDetail>>({});
  const [selectedId, setSelectedId] = useState<string>();
  const [error, setError] = useState<string>();
  const [isLoading, setIsLoading] = useState(true);
  const hasRunningSession = useMemo(
    () => Boolean(stats?.sessions.some((session) => session.status === 'running')),
    [stats?.sessions],
  );

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    getUsageStats()
      .then((loaded) => {
        if (cancelled) {
          return;
        }
        setStats(loaded);
        setSelectedId((current) => current || loaded.sessions[0]?.id);
        setError(undefined);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unable to load statistics.');
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId || sessionDetails[selectedId]) {
      return;
    }
    let cancelled = false;
    getSession(selectedId)
      .then((detail) => {
        if (!cancelled) {
          setSessionDetails((current) => ({ ...current, [detail.id]: detail }));
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unable to load session usage breakdown.');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, sessionDetails]);

  useEffect(() => {
    if (!hasRunningSession) {
      return;
    }

    let cancelled = false;

    const refreshLiveStats = async () => {
      try {
        const loaded = await getUsageStats();
        if (cancelled) {
          return;
        }
        setStats(loaded);
        setSelectedId((current) => current || loaded.sessions[0]?.id);
        setError(undefined);

        const selectedIsRunning = Boolean(
          selectedId && loaded.sessions.some((session) => session.id === selectedId && session.status === 'running'),
        );
        if (selectedIsRunning && selectedId) {
          const detail = await getSession(selectedId);
          if (!cancelled) {
            setSessionDetails((current) => ({ ...current, [detail.id]: detail }));
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unable to refresh live statistics.');
        }
      }
    };

    const interval = window.setInterval(refreshLiveStats, LIVE_STATS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [hasRunningSession, selectedId]);

  const selectedSummary = stats?.sessions.find((session) => session.id === selectedId);
  const selectedSession = useMemo<StatsSessionDetail | undefined>(() => {
    if (!selectedId) {
      return undefined;
    }
    const detail = sessionDetails[selectedId];
    if (detail && selectedSummary) {
      return {
        ...detail,
        ...selectedSummary,
        usage_breakdown: detail.usage_breakdown,
      };
    }
    return detail || selectedSummary;
  }, [selectedId, selectedSummary, sessionDetails]);

  return (
    <div className="lea-app stats-scope flex size-full flex-col bg-background text-foreground">
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-2.5">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back
        </button>
        <div className="h-4 w-px bg-border" />
        <span className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          Usage & Statistics
        </span>
        <div className="ml-auto flex min-w-0 items-center gap-2">
          {isLoading || hasRunningSession ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          ) : (
            <Cpu className="h-3.5 w-3.5 text-muted-foreground" />
          )}
          <span className="truncate font-mono text-[0.7rem] text-muted-foreground">
            {stats
              ? `${stats.global.session_count} sessions - $${stats.global.cost_usd.toFixed(2)} total${
                  hasRunningSession ? ' - live' : ''
                }`
              : 'Statistics'}
          </span>
        </div>
      </header>

      {error ? (
        <div className="m-6 rounded-md border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden xl:grid-cols-[260px_minmax(380px,1fr)_minmax(420px,1fr)]">
          <SessionListPane
            sessions={stats?.sessions || []}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          <SessionDetailPane session={selectedSession} />
          <GlobalStatsPane stats={stats} />
        </div>
      )}
    </div>
  );
}
