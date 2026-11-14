# Best Use of Gemma: Sous

_Status: in progress. The red-team evaluation that uses Gemma on a Verda GPU is running; this file is filled with real numbers when it finishes (see docs/REDTEAM.md). Nothing here is claimed until then._

**What Gemma does in Sous.** Gemma is the open-weight buyer agent in our large-scale red-team evaluation: served with vLLM on a Verda A100 in Finland, it plays thousands of Finnish and English kitchen buyers (normal orders and adversarial ones) against the real gateway, with Valio prices replayed from a snapshot. It shows that a buyer's purchasing agent can run on open weights inside the EU, and it measures, at a scale API rate limits don't allow, how often an agent tries something the buyer's mandate forbids and whether the gateway ever lets it through.
