const fs = require('fs');
const root = '/usr/local/lib/node_modules/n8n/node_modules/.pnpm';
const pkg = fs.readdirSync(root).find(x => x.startsWith('@n8n+n8n-nodes-langchain@'));
const base = `${root}/${pkg}/node_modules/@n8n/n8n-nodes-langchain/dist/nodes`;
const result = {};
for (const [path, name] of [['llms/LMChatOllama/LmChatOllama.node.js', 'LmChatOllama'], ['tools/ToolWorkflow/ToolWorkflow.node.js', 'ToolWorkflow'], ['agents/Agent/Agent.node.js', 'Agent']]) {
  const obj = new (require(`${base}/${path}`)[name])();
  result[name] = obj.nodeVersions ? Object.fromEntries(Object.entries(obj.nodeVersions).map(([v, n]) => [v, n.description])) : obj.description;
}
process.stdout.write(JSON.stringify(result, null, 2));
