import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Check, Eye, EyeOff, KeyRound, Plus, RotateCcw, Save, Settings, X } from 'lucide-react';
import {
  AppSettings,
  ApiKeyStatus,
  ModelCatalogEntry,
  ModelRequirements,
  PermissionTier,
  SettingsUpdate,
  fetchModelCatalog,
  fetchModelRequirements,
  getSettings,
  saveSettings,
} from '../lib/api';
import { ModelCombobox } from './ModelCombobox';
import { RadioGroup, RadioGroupItem } from './ui/radio-group';
import { Progress } from './ui/progress';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Separator } from './ui/separator';

// Client-side format hints for the first-class providers; other providers are
// validated by the backend / provider at runtime.
const KEY_PATTERNS: Record<string, RegExp> = {
  OPENAI_API_KEY: /^sk-[A-Za-z0-9_-]{8,}$/,
  ANTHROPIC_API_KEY: /^sk-ant-.+/,
  GOOGLE_API_KEY: /^AIza[A-Za-z0-9_-]{20,}$/,
};
const KEY_PLACEHOLDERS: Record<string, string> = {
  OPENAI_API_KEY: 'sk-...',
  ANTHROPIC_API_KEY: 'sk-ant-...',
  GOOGLE_API_KEY: 'AIza...',
  PORTKEY_API_KEY: 'Portkey API key',
};
// The Portkey gateway URL lives next to the Portkey key: it is the one provider
// whose endpoint is per-institution (a self-hosted gateway) rather than fixed.
const PORTKEY_KEY_ENV = 'PORTKEY_API_KEY';
const PORTKEY_BASE_URL_ENV = 'PORTKEY_BASE_URL';

interface KeyField {
  env: string;
  label: string;
  status?: ApiKeyStatus;
}

export function SettingsPage({ onBack }: { onBack: () => void }) {
  const [settings, setSettings] = useState<AppSettings>();
  const [model, setModel] = useState('');
  const [permissionTier, setPermissionTier] = useState<PermissionTier>('stepwise');
  const [maxSpend, setMaxSpend] = useState('');
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [clearedKeys, setClearedKeys] = useState<Record<string, boolean>>({});
  const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});
  // User-added providers beyond the first-class three (e.g. OPENROUTER_API_KEY):
  // editable {name, value} rows, saved as any `*_API_KEY` the backend accepts.
  const [customKeys, setCustomKeys] = useState<{ env: string; value: string }[]>([]);
  // Portkey gateway URL override. '' means "not saved in Settings" — the effective
  // value (shell export or the hosted default) is shown as the placeholder.
  const [portkeyBaseUrl, setPortkeyBaseUrl] = useState('');
  // GitHub token (D34) — redacted like a provider key; used by project "Push to GitHub".
  const [githubToken, setGithubToken] = useState('');
  const [githubVisible, setGithubVisible] = useState(false);
  const [clearGithub, setClearGithub] = useState(false);
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([]);
  const [requirements, setRequirements] = useState<ModelRequirements>();
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const load = async () => {
    setIsLoading(true);
    setError(undefined);
    try {
      const loaded = await getSettings();
      setSettings(loaded);
      setModel(loaded.model ?? '');
      setPermissionTier(loaded.permission_tier ?? 'stepwise');
      setMaxSpend(loaded.max_spend_usd == null ? '' : String(loaded.max_spend_usd));
      setApiKeys({});
      setClearedKeys({});
      setCustomKeys([]);
      setPortkeyBaseUrl(savedEndpointValue(loaded));
      setGithubToken('');
      setClearGithub(false);
      setFieldErrors({});
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load settings.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // The full model catalog is best-effort — a blank list just falls back to
    // the featured shortlist in the combobox.
    fetchModelCatalog()
      .then(setCatalog)
      .catch(() => setCatalog([]));
  }, []);

  // Look up which API key(s) the selected model needs, so the right field shows.
  useEffect(() => {
    if (!model.trim()) {
      setRequirements(undefined);
      return;
    }
    let cancelled = false;
    fetchModelRequirements(model)
      .then((req) => {
        if (!cancelled) setRequirements(req);
      })
      .catch(() => {
        if (!cancelled) setRequirements(undefined);
      });
    return () => {
      cancelled = true;
    };
  }, [model]);

  const currentSpend = settings?.current_spend_usd ?? 0;
  const maxSpendValue = Number(maxSpend);
  const spendPercent = maxSpendValue > 0 ? Math.min(100, (currentSpend / maxSpendValue) * 100) : 0;
  const spendTone =
    spendPercent >= 90
      ? '[&_[data-slot=progress-indicator]]:bg-destructive'
      : spendPercent >= 70
      ? '[&_[data-slot=progress-indicator]]:bg-yellow-500'
      : '[&_[data-slot=progress-indicator]]:bg-primary';

  // The API-key fields to render: every configured provider plus any key the
  // selected model requires (so a newly-needed provider's field appears).
  const keyFields = useMemo<KeyField[]>(() => {
    const fields: KeyField[] = [];
    const seen = new Set<string>();
    for (const [env, status] of Object.entries(settings?.api_keys || {})) {
      fields.push({ env, label: status.label, status });
      seen.add(env);
    }
    for (const req of requirements?.required_keys || []) {
      if (!seen.has(req.env)) {
        fields.push({ env: req.env, label: req.label });
        seen.add(req.env);
      }
    }
    return fields;
  }, [settings, requirements]);

  const requiredKeys = requirements?.required_keys || [];
  // A model's key requirement is a group: any one of the acceptable env vars
  // satisfies it (e.g. Gemini via GOOGLE_API_KEY or GEMINI_API_KEY).
  const keyMissing =
    requiredKeys.length > 0 &&
    !requiredKeys.some((k) => (k.configured && !clearedKeys[k.env]) || apiKeys[k.env]?.trim());
  // Only badge fields "Required" while the requirement is actually unmet.
  const requiredEnvs = useMemo(
    () => new Set(keyMissing ? requiredKeys.map((k) => k.env) : []),
    [keyMissing, requiredKeys],
  );

  const submit = async () => {
    setError(undefined);
    setFieldErrors({});
    setSaved(false);
    const localErrors = validateBeforeSave(keyFields, apiKeys, keyMissing, requiredKeys[0], model);
    // Validate the user-added providers: an env name like OPENROUTER_API_KEY + a
    // value. Empty rows are ignored; the cleaned list is what we send.
    const customClean: { env: string; value: string }[] = [];
    customKeys.forEach((row, i) => {
      const env = row.env.trim().toUpperCase();
      const value = row.value.trim();
      if (!env && !value) return;
      if (!/^[A-Z][A-Z0-9_]*_API_KEY$/.test(env)) {
        localErrors[`custom.${i}`] = 'Key name must look like OPENROUTER_API_KEY.';
      } else if (!value) {
        localErrors[`custom.${i}`] = 'Enter a value for this provider key.';
      } else {
        customClean.push({ env, value });
      }
    });
    if (Object.keys(localErrors).length > 0) {
      setFieldErrors(localErrors);
      setError(Object.values(localErrors)[0]);
      return;
    }
    setIsSaving(true);
    try {
      const apiKeyUpdates: NonNullable<SettingsUpdate['api_keys']> = {};
      for (const field of keyFields) {
        const value = (apiKeys[field.env] || '').trim();
        if (value) {
          apiKeyUpdates[field.env] = { value };
        } else if (clearedKeys[field.env]) {
          apiKeyUpdates[field.env] = { clear: true };
        }
      }
      for (const { env, value } of customClean) {
        apiKeyUpdates[env] = { value };
      }
      const githubUpdate = githubToken.trim()
        ? { value: githubToken.trim() }
        : clearGithub
        ? { clear: true }
        : undefined;
      // Only send the gateway URL when it changed: a saved value is replaced or
      // cleared, an inherited one is left alone.
      const savedGateway = savedEndpointValue(settings);
      const nextGateway = portkeyBaseUrl.trim();
      const endpointUpdate =
        nextGateway === savedGateway
          ? undefined
          : nextGateway
          ? { value: nextGateway }
          : { clear: true };
      const update: SettingsUpdate = {
        model,
        permission_tier: permissionTier,
        max_spend_usd: maxSpend.trim() ? Number(maxSpend) : null,
        api_keys: Object.keys(apiKeyUpdates).length ? apiKeyUpdates : undefined,
        provider_endpoints: endpointUpdate ? { [PORTKEY_BASE_URL_ENV]: endpointUpdate } : undefined,
        github_token: githubUpdate,
      };
      const savedSettings = await saveSettings(update);
      setSettings(savedSettings);
      setModel(savedSettings.model ?? '');
      setPermissionTier(savedSettings.permission_tier ?? 'stepwise');
      setMaxSpend(savedSettings.max_spend_usd == null ? '' : String(savedSettings.max_spend_usd));
      setApiKeys({});
      setClearedKeys({});
      setCustomKeys([]);
      setPortkeyBaseUrl(savedEndpointValue(savedSettings));
      setGithubToken('');
      setClearGithub(false);
      setFieldErrors({});
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1800);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unable to save settings.';
      const field = err instanceof Error ? (err as Error & { field?: string }).field : undefined;
      if (field) {
        setFieldErrors({ [field]: message });
      }
      setError(message);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="lea-app settings-scope min-h-full bg-background text-foreground">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 py-8">
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Settings className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl font-semibold text-foreground">Settings</h1>
              <p className="text-sm text-muted-foreground">Configure models, approval behavior, and run limits.</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onBack}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            aria-label="Close settings"
            title="Close settings"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-foreground">
            {error}
          </div>
        )}

        {isLoading ? (
          <div className="rounded-md border border-border bg-card px-4 py-8 text-center text-sm text-muted-foreground">
            Loading settings...
          </div>
        ) : (
          <div className="rounded-md border border-border bg-card">
            <Section
              title="Model"
              description="Backend model used for proof formalization. Search the full provider catalog or type any model ID — you'll be prompted for that provider's API key below."
            >
              <ModelCombobox
                value={model}
                onChange={setModel}
                catalog={catalog}
                featured={settings?.model_options || []}
              />
            </Section>

            <Separator />

            <Section title="API Keys" description="Configured keys stay in the local backend config. The key for the selected model is required.">
              <div className="space-y-4">
                {keyFields.map((field) => {
                  const status = field.status;
                  const configured = Boolean(status?.configured) && !clearedKeys[field.env];
                  const placeholder = KEY_PLACEHOLDERS[field.env] || 'Enter API key';
                  return (
                    <div key={field.env} className="space-y-2">
                      <div className="flex items-center justify-between gap-3">
                        <Label htmlFor={`${field.env}-api-key`} className="flex items-center gap-2">
                          {field.label}
                          {requiredEnvs.has(field.env) && (
                            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase text-primary">
                              Required
                            </span>
                          )}
                        </Label>
                        {configured && (
                          <span className="flex items-center gap-1 text-xs text-muted-foreground">
                            <KeyRound className="h-3.5 w-3.5" />
                            Saved{status?.last4 ? ` ...${status.last4}` : ''}
                          </span>
                        )}
                      </div>
                      <div className="flex gap-2">
                        <div className="relative flex-1">
                          <Input
                            id={`${field.env}-api-key`}
                            type={visibleKeys[field.env] ? 'text' : 'password'}
                            value={apiKeys[field.env] || ''}
                            placeholder={configured ? 'Enter a new key to replace the saved key' : placeholder}
                            onChange={(event) =>
                              setApiKeys((current) => ({ ...current, [field.env]: event.target.value }))
                            }
                            className="pr-10 font-mono"
                          />
                          <button
                            type="button"
                            onClick={() =>
                              setVisibleKeys((current) => ({ ...current, [field.env]: !current[field.env] }))
                            }
                            className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                            aria-label={`Toggle ${field.label} key visibility`}
                            title={`Toggle ${field.label} key visibility`}
                          >
                            {visibleKeys[field.env] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                          </button>
                        </div>
                        {status?.configured && (
                          <button
                            type="button"
                            onClick={() =>
                              setClearedKeys((current) => ({ ...current, [field.env]: !current[field.env] }))
                            }
                            className={[
                              'flex h-9 w-9 items-center justify-center rounded-md border transition-colors',
                              clearedKeys[field.env]
                                ? 'border-destructive/40 bg-destructive/10 text-destructive'
                                : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
                            ].join(' ')}
                            aria-label={clearedKeys[field.env] ? `Keep ${field.label} key` : `Clear ${field.label} key`}
                            title={clearedKeys[field.env] ? `Keep ${field.label} key` : `Clear ${field.label} key`}
                          >
                            {clearedKeys[field.env] ? <RotateCcw className="h-4 w-4" /> : <X className="h-4 w-4" />}
                          </button>
                        )}
                      </div>
                      {fieldErrors[`api_keys.${field.env}`] && (
                        <p className="text-xs text-destructive">{fieldErrors[`api_keys.${field.env}`]}</p>
                      )}
                      {clearedKeys[field.env] && (
                        <p className="text-xs text-destructive">This saved key will be removed on save.</p>
                      )}
                      {field.env === PORTKEY_KEY_ENV && (
                        <div className="space-y-1.5 pt-1">
                          <Label htmlFor="portkey-base-url" className="text-xs text-muted-foreground">
                            Gateway URL
                          </Label>
                          <Input
                            id="portkey-base-url"
                            type="url"
                            value={portkeyBaseUrl}
                            placeholder={
                              settings?.provider_endpoints?.[PORTKEY_BASE_URL_ENV]?.value ||
                              'https://api.portkey.ai/v1'
                            }
                            onChange={(event) => setPortkeyBaseUrl(event.target.value)}
                            className="font-mono"
                          />
                          <p className="text-xs text-muted-foreground">
                            Your Portkey gateway&apos;s <code>/v1</code> root — a self-hosted gateway such as
                            your institution&apos;s, or Portkey&apos;s hosted service when blank.
                            {settings?.provider_endpoints?.[PORTKEY_BASE_URL_ENV]?.source === 'env' &&
                              !portkeyBaseUrl.trim() &&
                              ' Currently taken from the PORTKEY_BASE_URL environment variable.'}{' '}
                            Model IDs look like <code>portkey/@provider-slug/model</code>.
                          </p>
                          {fieldErrors[`provider_endpoints.${PORTKEY_BASE_URL_ENV}`] && (
                            <p className="text-xs text-destructive">
                              {fieldErrors[`provider_endpoints.${PORTKEY_BASE_URL_ENV}`]}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}

                {customKeys.map((row, i) => (
                  <div key={`custom-${i}`} className="space-y-2 rounded-md border border-dashed border-border p-3">
                    <div className="flex items-center justify-between gap-3">
                      <Label className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                        Custom provider
                      </Label>
                      <button
                        type="button"
                        onClick={() => setCustomKeys((cur) => cur.filter((_, j) => j !== i))}
                        className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                        aria-label="Remove this provider"
                        title="Remove this provider"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                    <div className="flex gap-2">
                      <Input
                        value={row.env}
                        placeholder="OPENROUTER_API_KEY"
                        onChange={(event) =>
                          setCustomKeys((cur) =>
                            cur.map((c, j) => (j === i ? { ...c, env: event.target.value.toUpperCase() } : c)),
                          )
                        }
                        aria-label="Provider key name"
                        className="w-1/2 font-mono"
                      />
                      <Input
                        type="password"
                        value={row.value}
                        placeholder="Enter API key"
                        onChange={(event) =>
                          setCustomKeys((cur) => cur.map((c, j) => (j === i ? { ...c, value: event.target.value } : c)))
                        }
                        aria-label="Provider key value"
                        className="flex-1 font-mono"
                      />
                    </div>
                    {fieldErrors[`custom.${i}`] && (
                      <p className="text-xs text-destructive">{fieldErrors[`custom.${i}`]}</p>
                    )}
                  </div>
                ))}

                <button
                  type="button"
                  onClick={() => setCustomKeys((cur) => [...cur, { env: '', value: '' }])}
                  className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  <Plus className="h-4 w-4" /> Add another provider
                </button>
              </div>
            </Section>

            <Separator />

            <Section
              title="GitHub"
              description="A token to push a project to GitHub (from a project's Filesystem tab). Needs repo scope."
            >
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor="github-token">Personal access token</Label>
                  {settings?.github_token?.configured && !clearGithub && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                      <KeyRound className="h-3.5 w-3.5" />
                      Saved{settings.github_token.last4 ? ` ...${settings.github_token.last4}` : ''}
                    </span>
                  )}
                </div>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Input
                      id="github-token"
                      type={githubVisible ? 'text' : 'password'}
                      value={githubToken}
                      placeholder={
                        settings?.github_token?.configured && !clearGithub
                          ? 'Enter a new token to replace the saved one'
                          : 'ghp_… or github_pat_…'
                      }
                      onChange={(event) => setGithubToken(event.target.value)}
                      className="pr-10 font-mono"
                    />
                    <button
                      type="button"
                      onClick={() => setGithubVisible((v) => !v)}
                      className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                      aria-label="Toggle GitHub token visibility"
                      title="Toggle GitHub token visibility"
                    >
                      {githubVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                  {settings?.github_token?.configured && (
                    <button
                      type="button"
                      onClick={() => setClearGithub((c) => !c)}
                      className={[
                        'flex h-9 w-9 items-center justify-center rounded-md border transition-colors',
                        clearGithub
                          ? 'border-destructive/40 bg-destructive/10 text-destructive'
                          : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
                      ].join(' ')}
                      aria-label={clearGithub ? 'Keep GitHub token' : 'Clear GitHub token'}
                      title={clearGithub ? 'Keep GitHub token' : 'Clear GitHub token'}
                    >
                      {clearGithub ? <RotateCcw className="h-4 w-4" /> : <X className="h-4 w-4" />}
                    </button>
                  )}
                </div>
                {fieldErrors['github_token'] && (
                  <p className="text-xs text-destructive">{fieldErrors['github_token']}</p>
                )}
                {clearGithub && (
                  <p className="text-xs text-destructive">This saved token will be removed on save.</p>
                )}
              </div>
            </Section>

            <Separator />

            <Section title="Permission Tier" description="Controls where the agent pauses for approval.">
              <RadioGroup
                value={permissionTier}
                onValueChange={(value) => setPermissionTier(value as PermissionTier)}
                className="gap-2"
              >
                {(settings?.permission_tiers || []).map((tier) => (
                  <Label
                    key={tier.value}
                    className={[
                      'flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors',
                      permissionTier === tier.value ? 'border-primary bg-accent' : 'border-border hover:bg-accent/70',
                    ].join(' ')}
                  >
                    <RadioGroupItem value={tier.value} className="mt-0.5" />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-foreground">{tier.label}</span>
                      <span className="mt-1 block text-sm font-normal text-muted-foreground">{tier.description}</span>
                    </span>
                  </Label>
                ))}
              </RadioGroup>
            </Section>

            <Separator />

            <Section title="Max Spend" description="Global spend cap across recorded runs.">
              <div className="space-y-4">
                <div className="flex items-center gap-3">
                  <div className="relative flex-1">
                    <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
                      $
                    </span>
                    <Input
                      type="number"
                      min={0}
                      step="0.01"
                      value={maxSpend}
                      onChange={(event) => setMaxSpend(event.target.value)}
                      placeholder="No cap"
                      className="pl-7"
                    />
                  </div>
                  <div className="w-36 text-right text-sm text-muted-foreground">
                    ${currentSpend.toFixed(2)} used
                  </div>
                </div>
                <div className="space-y-2">
                  <Progress value={spendPercent} className={spendTone} />
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>{spendPercent.toFixed(0)}% of cap</span>
                    <span>{maxSpendValue > 0 ? `$${maxSpendValue.toFixed(2)}` : 'No cap set'}</span>
                  </div>
                </div>
              </div>
            </Section>

            <div className="flex items-center justify-end gap-3 border-t border-border p-5">
              {keyMissing && requiredKeys[0] && (
                <span className="text-sm text-muted-foreground">{requiredKeys[0].label} key required for this model.</span>
              )}
              {saved && (
                <span className="flex items-center gap-1 text-sm text-green-600">
                  <Check className="h-4 w-4" />
                  Saved
                </span>
              )}
              <button
                type="button"
                onClick={submit}
                disabled={isSaving || !model}
                className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Save className="h-4 w-4" />
                {isSaving ? 'Saving...' : 'Save Settings'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="grid gap-4 p-5 md:grid-cols-[minmax(160px,220px)_1fr]">
      <div>
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{description}</p>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

// The gateway URL as saved in Settings ('' when the effective value is inherited
// from the shell or the default), so the field starts out matching the file.
function savedEndpointValue(settings: AppSettings | undefined): string {
  const endpoint = settings?.provider_endpoints?.[PORTKEY_BASE_URL_ENV];
  return endpoint?.source === 'config' ? endpoint.value : '';
}

function validateBeforeSave(
  keyFields: KeyField[],
  apiKeys: Record<string, string>,
  keyMissing: boolean,
  firstRequired: { env: string; label: string } | undefined,
  model: string,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of keyFields) {
    const value = (apiKeys[field.env] || '').trim();
    const pattern = KEY_PATTERNS[field.env];
    if (value && pattern && !pattern.test(value)) {
      errors[`api_keys.${field.env}`] = `The ${field.label} API key does not look valid. Check the key and try again.`;
    }
  }
  if (keyMissing && firstRequired) {
    errors[`api_keys.${firstRequired.env}`] = `Add a ${firstRequired.label} API key before saving ${model}.`;
  }
  return errors;
}
