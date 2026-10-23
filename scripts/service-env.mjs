// Public HTTP/UI roles never receive provider execution credentials.
export function serviceEnvironment(role, source = process.env) {
  const env = {...source};
  if (role !== 'worker') {
    for (const name of Object.keys(env)) {
      if (/_API_KEY$|AUTH_TOKEN|GITHUB_TOKEN|PASSWORD|SECRET/i.test(name)) delete env[name];
    }
  }
  return env;
}
