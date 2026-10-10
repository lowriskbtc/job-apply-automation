#!/usr/bin/env python
"""collect_boards.py - pull open roles from public Greenhouse/Ashby/Lever board APIs.
No login. Writes apply URLs (one per line) filtered by title keywords.
  python collect_boards.py --out jobs_boards.txt
"""
import json, re, sys, urllib.request, concurrent.futures as cf
CRYPTO = "coinbase kraken consensys chainlink-labs phantom alchemy anchorage ripple okx moonpay uniswaplabs opensea dydx paradigm fireblocks chainalysis circle gemini bitgo blockchain-com ledger polygon-labs solana-labs jito-labs helius drift-labs jupiter-exchange marinade wormhole-foundation layerzero-labs eigen-labs ethereum-foundation flashbots offchainlabs optimism matter-labs starkware aptoslabs mysten-labs near aave compound-labs lido figment anza magiceden bitgo galaxydigitalservices paxos zerohash blockdaemon alchemyplatform thirdweb dune chainstack tenderly nansen etherscan cow-dao conduit categorylabs".split()
AI = "anthropic openai scaleai cohere perplexityai mistral huggingface runwayml elevenlabs together-ai fireworks-ai anyscale modal labs-lambda weightsandbiases pinecone weaviate langchain llamaindex cursor harvey sierra decagon glean writer jasper replit codeium sourcegraph vercel databricks livekit anduril hippocratic-ai clickup stripe xai deepmind character cerebras groq baseten replicate zapier ramp brex mercury arizeai langfuse braintrust".split()

EXTRA_CRYPTO = "coinbase kraken chainlinklabs chainlink phantom alchemy ripple fireblocks circle gemini bitgo ledger dydx uniswaplabs opensea magiceden jito jitolabs driftprotocol jupiter helius solanafoundation solana anza eigenlabs lido aave compoundlabs polygon polygonlabs matterlabs starkware aptoslabs aptos mystenlabs sui near nearfoundation figment blockdaemon thirdweb dune nansen tenderly chainalysis anchorage paxos zerohash bitso bullish uphold bitstamp crypto cryptocom okx bybit kucoin gate bitfinex deribit hyperliquid hyperliquidlabs raydium orca meteora marginfi kamino solend phoenix wormhole layerzero axelar celestia eigenlayer symbiotic ethena maker sky makerdao curve convex yearn synthetix gmx pendle ondo centrifuge maple goldfinch securitize fireblocks copper talos wintermute gsr cumberland dwf jumptrading flowdesk keyrock b2c2 amberdata kaiko coinmetrics messari artemis defillama dexscreener birdeye pumpfun bonk jupiterexchange tensor sanctum squads backpack coinbasecloud base worldcoin tools-for-humanity ethereumfoundation flashbots offchainlabs arbitrum optimism op-labs consensys metamask infura linea safe gnosis ens ensdomains nethermind reth paradigm a16zcrypto dragonfly polychain multicoin framework pantera galaxy galaxydigital".split()
EXTRA_AI = "perplexityai perplexity mistral mistralai huggingface runwayml runway together togetherai fireworksai fireworks lambda lambdalabs wandb weightsandbiases pinecone weaviate langchain replit sourcegraph glean sierra decagon hebbia cresta abridge assemblyai deepgram poolside anysphere humeai hume adept cognition windsurf codeium tabnine snorkelai labelbox scale clay retool notion figma airtable linear mercury plaid rippling deel gusto samsara anduril shieldai saronic hadrian castelion applied-intuition appliedintuition waymo aurora nuro zoox tesla neuralink openai xai anthropic cohere characterai character inflection stabilityai midjourney pika luma lumalabs ideogram suno udio elevenlabs descript synthesia heygen captions veed typeface jasper copyai writer anyword tome gamma beautifulai canva grammarly quillbot duolingo khan speak praktika modal baseten replicate banana coreweave crusoe vastai lambda cerebras groq sambanova graphcore etched tenstorrent d-matrix lightmatter celestial rain-ai mythic sima hailo".split()


EXTRA2 = """anthropic openai scale snorkel mosaicml databricks cohere adept ai21 aleph-alpha writer jasper runway pika midjourney leonardo krea black-forest-labs bfl lightricks stabilityai civitai replicate fal falai modal baseten anyscale octoml deci neuralmagic predibase lamini unstructured llamaindex langchain pinecone weaviate qdrant chroma zilliz milvus vespa elastic mongodb supabase neon planetscale cockroachlabs singlestore clickhouse materialize confluent redpanda dbt fivetran airbyte census hightouch rudderstack segment amplitude mixpanel posthog heap fullstory launchdarkly statsig eppo optimizely split growthbook sentry datadog honeycomb grafana chronosphere cribl observeinc lightstep newrelic dynatrace pagerduty incidentio rootly firehydrant cortex port backstage spotify opslevel harness circleci buildkite gitlab github sourcegraph graphite linear height shortcut jira asana monday clickup notion coda airtable retool appsmith budibase zapier make tray workato n8n temporal prefect dagster airflow astronomer anyscale ray determined wandb weightsandbiases comet neptune clearml arize arizeai fiddler truera whylabs galileo humanloop promptlayer braintrust langfuse helicone portkey traceloop vellum dust glean hebbia harvey casetext spellbook ironclad evisort luminance eudia legora robin norm-ai sierra decagon parloa cresta observe-ai uniphore replicant cognigy kore moveworks aisera forethought ada intercom zendesk gorgias front helpscout plain pylon thena mutiny clay apollo outreach salesloft gong chorus clari people-ai regie 11x artisan aisdr unify warmly 6sense demandbase zoominfo clearbit sumble harmonic pitchbook crunchbase ramp brex mercury divvy bill airwallex wise remitly flywire payoneer marqeta lithic highnote galileo-ft unit treasuryprime column increase moov modern-treasury modernTreasury moderntreasury lendflow pipe capchase ribbit carta pulley angellist sydecar mesh finicity akoya yodlee plaid teller stripe adyen checkout rapyd paddle lemonsqueezy chargebee recurly maxio zuora orb metronome lago stigg schematic togai amberflo"""
EXTRA3 = """kraken coinbase gemini bitgo anchorage paxos circle ripple consensys chainalysis elliptic trmlabs trm-labs merkle-science coinfirm crystalblockchain alchemy infura quicknode ankr moralis thirdweb dynamic privy magic web3auth turnkey fireblocks copper cobo bitgo liminal dfns sepior zengo ledger trezor phantom solflare backpack rainbow metamask coinbasewallet uniswap sushi curve balancer 1inch paraswap cowswap cow-protocol matcha 0x zerox kyber bancor dydx gmx perp perpetualprotocol vertex synthetix kwenta lyra aevo hyperliquid drift jupiter raydium orca meteora kamino marginfi solend marinade jito sanctum helius triton rpcpool shyft quicknode-solana anza solana-foundation solanalabs solana-labs eclipse neon-evm sonic backpack-exchange tensor magiceden coralcube metaplex helium render nosana akash io-net gensyn bittensor opentensor ritual allora sentient prime-intellect nous-research pluralis flock fetch-ai singularitynet ocean numerai ethereum-foundation ef optimism arbitrum offchain-labs polygon matter-labs zksync starkware starknet scroll linea base blast mantle mode zora celo gnosis safe-global ens lido rocketpool eigenlayer eigen-labs symbiotic karak ether-fi renzo kelp puffer swell stakewise ssv obol figment kiln chorus-one staked p2p blockdaemon coinbase-cloud alchemy-pay moonpay transak ramp-network banxa mtpelerin sardine persona alloy sumsub veriff onfido jumio trulioo"""

slugs = sorted(set(EXTRA2.split()+EXTRA3.split()+CRYPTO+AI+EXTRA_CRYPTO+EXTRA_AI+(open('extra_slugs.txt').read().split() if '--extra-only' not in sys.argv else [])))
if '--extra-only' in sys.argv: slugs = sorted(set(open('extra_slugs.txt').read().split()))
TITLE = re.compile(r"\b(ai|ml|machine learning|llm|agent|agents|applied scientist|research engineer|solana|blockchain|smart contract|protocol|web3|crypto|defi|rust|solidity|onchain|on-chain|mev|trading|infrastructure|backend|software engineer|full.?stack)\b", re.I)
BAD = re.compile(r"\b(intern|manager|director|sales|recruit|counsel|marketing|designer|account|support|legal|finance|security analyst|ios|android|mobile|staff product|principal|vp|head)\b", re.I)
def get(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=15) as r: return json.load(r)
    except Exception: return None
def one(s):
    out=[]
    d=get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
    if d:
        for j in d.get("jobs",[]):
            out.append((s,j["title"],j.get("location",{}).get("name",""),f"https://job-boards.greenhouse.io/{s}/jobs/{j['id']}"))
    d=get(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
    if d:
        for j in d.get("jobs",[]):
            out.append((s,j["title"],j.get("location",""),j["jobUrl"]))
    d=get(f"https://api.lever.co/v0/postings/{s}?mode=json")
    if isinstance(d,list):
        for j in d: out.append((s,j["text"],j.get("categories",{}).get("location",""),j["hostedUrl"]))
    return out
def main():
    out=sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else "jobs_boards.txt"
    rows=[]
    with cf.ThreadPoolExecutor(12) as ex:
        for r in ex.map(one,slugs): rows+=r
    keep=[r for r in rows if TITLE.search(r[1]) and not BAD.search(r[1])
          and re.search(r"remote|united states|usa|us\b|san francisco|new york|sf|nyc|anywhere",r[2] or "remote",re.I)]
    print(len(rows),"open,",len(keep),"kept")
    with open(out,"w") as f:
        for r in keep: f.write(r[3]+"\n")
    json.dump(keep,open(out+".json","w"),indent=0)
main()
