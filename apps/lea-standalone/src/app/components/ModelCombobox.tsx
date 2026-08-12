import { useMemo, useState } from 'react';
import { Check, ChevronsUpDown } from 'lucide-react';
import type { ModelCatalogEntry, ModelOption } from '../lib/api';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from './ui/command';

// Cap rendered rows — the LiteLLM catalog has ~2k models; mounting them all is slow.
const MAX_RESULTS = 50;

/**
 * Searchable, creatable model picker over the full LiteLLM catalog. Typing
 * filters across model ID + provider; a non-matching entry can be used directly
 * (the backend infers the provider and prompts for its key). When empty it shows
 * the curated featured shortlist so common picks stay one click away.
 */
export function ModelCombobox({
  value,
  onChange,
  catalog,
  featured,
}: {
  value: string;
  onChange: (value: string) => void;
  catalog: ModelCatalogEntry[];
  featured: ModelOption[];
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');

  const selectedLabel = useMemo(
    () => catalog.find((m) => m.value === value)?.value ?? value,
    [catalog, value],
  );

  const trimmed = query.trim();
  const results = useMemo<ModelCatalogEntry[]>(() => {
    if (!trimmed) {
      return featured.map((m) => ({ value: m.value, label: m.label, provider: m.family ?? '' }));
    }
    const q = trimmed.toLowerCase();
    return catalog
      .filter((m) => m.value.toLowerCase().includes(q) || m.provider.toLowerCase().includes(q))
      .slice(0, MAX_RESULTS);
  }, [catalog, featured, trimmed]);

  const isKnown = catalog.some((m) => m.value === trimmed) || featured.some((m) => m.value === trimmed);

  const choose = (next: string) => {
    onChange(next);
    setQuery('');
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          className="flex h-9 w-full items-center justify-between gap-2 rounded-md border bg-background px-3 py-2 text-sm font-normal text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="truncate">{value ? selectedLabel : 'Select or type a model'}</span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      {/* The popover portals to <body>, outside the page's theme scope, so carry the
          Lea shadcn palette on the content itself (`.settings-scope` defines the
          vars). Without this the dropdown falls back to the generic theme. */}
      <PopoverContent className="settings-scope w-[var(--radix-popover-trigger-width)] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search models or type any model ID…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            {trimmed && !isKnown && (
              <CommandGroup heading="Custom">
                <CommandItem value={`custom:${trimmed}`} onSelect={() => choose(trimmed)}>
                  <Check className={`mr-2 h-4 w-4 ${value === trimmed ? 'opacity-100' : 'opacity-0'}`} />
                  Use <span className="ml-1 font-mono">{trimmed}</span>
                </CommandItem>
              </CommandGroup>
            )}
            <CommandGroup heading={trimmed ? 'Matches' : 'Featured'}>
              {results.map((m) => (
                <CommandItem key={m.value} value={m.value} onSelect={() => choose(m.value)}>
                  <Check className={`mr-2 h-4 w-4 ${value === m.value ? 'opacity-100' : 'opacity-0'}`} />
                  <span className="flex-1 truncate font-mono text-xs">{m.value}</span>
                  {m.provider && (
                    <span className="ml-2 shrink-0 text-xs text-muted-foreground">{m.provider}</span>
                  )}
                </CommandItem>
              ))}
            </CommandGroup>
            {results.length === 0 && <CommandEmpty>Type a model ID to use it directly.</CommandEmpty>}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
