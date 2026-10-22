import React from 'react';
import { createRoot } from 'react-dom/client';
import * as monaco from 'monaco-editor';
import { LiveEditor } from '../../../apps/lea-standalone/src/app/components/LiveEditor';
import { writeSessionFile } from '../../../apps/lea-standalone/src/app/lib/api';
import '../../../apps/lea-standalone/src/styles/index.css';
import '../../../apps/lea-standalone/src/styles/lea-v2.css';

// Let Vite resolve the same React/Monaco modules and styles as the application.
// Importing an unversioned optimizer cache URL creates a second Monaco instance.
export function mountEditorFixture(saving = false) {
  document.body.innerHTML = '<main id="editor-fixture" style="height:800px;display:flex"></main>';
  const root = createRoot(document.querySelector('main')!);
  const renderEditor = (sessionId = 'session') => root.render(<LiveEditor
    sessionId={sessionId} path="Main.lean" captureSelection={() => () => true}
    {...(saving ? {
      onSave: (content, path, base, draftId) => writeSessionFile(base.session_id, path, content,
        undefined, base.formalization_id, base.formalization_revision, base.file_revision, draftId),
      onCheck: async () => { throw new Error('Check service unavailable'); },
    } : {})}
  />);
  Object.assign(window, { renderEditor, fixtureMonaco: monaco });
  renderEditor();
}
