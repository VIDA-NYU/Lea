
  import { createRoot } from "react-dom/client";
  import App from "./app/App.tsx";
  import "./styles/index.css";
  import "./styles/lea-v2.css";
  import { assertDatabaseBoundary } from '../../../packages/lea-workspace-client/index.mjs';

  const root=createRoot(document.getElementById("root")!);
  async function start() {
    try {
      const response=await fetch('/api/health',{signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw new Error('The API is unavailable.');
      assertDatabaseBoundary(await response.json());
      root.render(<App/>);
    } catch(error) {
      root.render(<div role="alert"><p>{error instanceof Error?error.message:'Unable to connect to the API.'}</p><button onClick={()=>void start()}>Retry connection</button></div>);
    }
  }
  void start();
