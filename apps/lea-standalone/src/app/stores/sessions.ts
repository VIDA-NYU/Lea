import { create } from 'zustand';
import { sessionListSnapshot, type SessionSummary } from '../lib/api';

/**
 * The sessions store (v2.0.1 R3) — the session list + which one is open.
 *
 * Sidebar reads the list straight from here; App reads the selection for its
 * handlers; useProofStream pulls `setSelectedSessionId` + `refreshSessions` from
 * here (so the hook no longer needs them passed in).
 */
interface SessionsState {
  sessions: SessionSummary[];
  cursor?: number;
  setSessions: (sessions: SessionSummary[]) => void;
  selectedSessionId?: string;
  setSelectedSessionId: (id?: string) => void;
  // Reload the list from the API and return it (callers use the result to, e.g.,
  // restore the last-open session on startup).
  refreshSessions: () => Promise<SessionSummary[]>;
}

let refreshVersion = 0;
export const useSessions = create<SessionsState>((set) => ({
  sessions: [],
  setSessions: (sessions) => set({ sessions }),
  selectedSessionId: undefined,
  setSelectedSessionId: (selectedSessionId) => set({ selectedSessionId }),
  refreshSessions: async () => {
    const version = ++refreshVersion;
    const snapshot = await sessionListSnapshot();
    // Request ownership handles delayed responses. A fresh server snapshot may
    // legitimately have a lower watermark after restoring a database backup.
    if (version === refreshVersion) {
      set(snapshot);
    }
    return snapshot.sessions;
  },
}));
