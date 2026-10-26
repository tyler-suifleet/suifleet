// The browser confirms each bounded step through useFleetTransaction. A failed
// step stops immediately; only an explicit notFound read completes cleanup.
async function cleanupDeployment(deployment, fleet, step) {
  for (let n = 0; n < 100; n++) {
    const result = await step(deployment, fleet);
    if (result.done) return;
  }
  throw Error('Deployment cleanup did not finish in 100 bounded steps');
}
module.exports = { cleanupDeployment };
