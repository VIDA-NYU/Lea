// Fixed package assets used by Lean InfoView. No glob parser or runtime watcher.
import fs from 'node:fs';
import path from 'node:path';

export function leanAssets({infoviewDist, lean4monacoDist}) {
  const files = new Map();
  for (const name of fs.readdirSync(infoviewDist, {recursive:true})) {
    const source = path.join(infoviewDist,name);
    if (fs.statSync(source).isFile()) files.set(`/infoview/${name.split(path.sep).join('/')}`,source);
  }
  files.set('/infoview/webview.js',path.join(lean4monacoDist,'webview/webview.js'));
  files.set('/assets/codicon.ttf',path.join(infoviewDist,'codicon.ttf'));
  let outDir;
  return {
    name:'lea-infoview-assets',
    configResolved(config) {outDir=path.resolve(config.root,config.build.outDir);},
    configureServer(server) {
      server.middlewares.use((req,_res,next) => {
        const source = files.get((req.url || '').split('?')[0]);
        if (source) req.url = `/@fs/${source.split(path.sep).join('/')}`;
        next();
      });
    },
    closeBundle() {
      for (const [url,source] of files) {
        const target = path.join(outDir,url.slice(1));
        fs.mkdirSync(path.dirname(target),{recursive:true});
        fs.copyFileSync(source,target);
      }
    },
  };
}
