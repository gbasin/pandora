import { startInstance } from '/workspace/source/tools/stack/instance.mjs';
import { loadJourneys } from '/workspace/source/packages/scenarios/src/journeys/index.ts';
import { runJourney } from '/workspace/source/packages/scenarios/src/runner.ts';
import { checkRoutes } from '/workspace/source/packages/scenarios/src/cli/plan.ts';
const controller = new AbortController();
const stop=()=>controller.abort();
process.on('SIGTERM',stop);process.on('SIGINT',stop);
let stack;
try {
  stack=await startInstance({external:true,signal:controller.signal,output:()=>{}});
  const journey=(await loadJourneys()).find(j=>j.id==='S0-01');
  const result=await runJourney(journey,{baseUrl:stack.api,printPrincipals:false});
  if(result.status==='pass' && result.writeRoutes) await checkRoutes(result.id,result.writeRoutes,false);
  console.log(JSON.stringify({journey:result.id,status:result.status,detail:result.detail}));
  process.exitCode=result.status==='pass'?0:1;
} catch(error) { console.error(error.message);process.exitCode=1; }
finally { if(stack) await stack.stop(); }
