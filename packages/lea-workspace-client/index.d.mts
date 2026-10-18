export function workspaceResult(initial: any, read: (id: string) => Promise<any>, options?: { wait?: (ms: number) => Promise<void>; timeoutMs?: number }): Promise<any>;
export function submitWithRecovery(send: () => Promise<any>, lookup: () => Promise<any>): Promise<any>;
export function assertDatabaseBoundary(health: unknown): { version: 1; ui_access: 'database'; command_direction: 'ui_to_worker'; processes: 'separate'; queries: 1; changes: 1; commands: 1; source_publications: 1; editor: 1 };
