schema name test.crawl.job.0
schema id 0x3a313ac272bfaa668623c2af0ef4a77dfdccc92bc46cfe78235f5fbeef57c90f
cid bafkreicow3s3sulrbgcmrsfvtpu57ayn6eg3tcug55ocpzd2nz5gsyhb2y

---

this is for testing the crawler running by itself

First we need to install the cmon lib in an isolated environment. We do this because it has older, conflicting deps that we want to avoid intermingling with the core requirements.txt.

```
# Create the virtual environment
python3 -m venv cmon_venv

# Activate it (Mac/Linux)
source cmon_venv/bin/activate
# Or on Windows:
# cmon_venv\Scripts\activate

# Install cmoncrawl into this isolated space
pip install cmoncrawl

# Deactivate to return to your main quickbeam environment
deactivate
```

We can get lots of recipes ;)

foods blogs

--url https://smittenkitchen.com/
--url https://www.recipetineats.com/
--url https://www.loveandlemons.com/
--url https://cookieandkate.com/
--url https://minimalistbaker.com/
--url https://www.feastingathome.com/
--url https://www.gimmesomeoven.com/
--url https://www.wellplated.com/
--url https://www.spendwithpennies.com/
--url https://www.themediterraneandish.com/
--url https://www.halfbakedharvest.com/
--url https://damndelicious.net/
--url https://natashaskitchen.com/
--url https://www.jocooks.com/
--url https://www.twopeasandtheirpod.com/
--url https://www.chelseasmessyapron.com/
--url https://www.aheadofthyme.com/
--url https://www.jessicagavin.com/
--url https://www.acouplecooks.com/
--url https://pinchofyum.com/

baking websites
--url https://sallysbakingaddiction.com/
--url https://www.kingarthurbaking.com/
--url https://preppykitchen.com/
--url https://www.lifeloveandsugar.com/
--url https://www.biggerbolderbaking.com/
--url https://www.handletheheat.com/
--url https://sugarspunrun.com/
--url https://www.livewellbakeoften.com/
--url https://www.browneyedbaker.com/

bbq/grilling

--url https://amazingribs.com/
--url https://heygrillhey.com/
--url https://girlscangrill.com/
--url https://www.vindulge.com/
--url https://www.smokedbbqsource.com/

veg/vegan
--url https://minimalistbaker.com/
--url https://cookieandkate.com/
--url https://ohsheglows.com/
--url https://rainbowplantlife.com/
--url https://www.noracooks.com/
--url https://itdoesnttastelikechicken.com/
--url https://www.loveandlemons.com/


meal prep
--url https://www.skinnytaste.com/
--url https://www.eatingwell.com/
--url https://www.wellplated.com/
--url https://www.ambitiouskitchen.com/
--url https://downshiftology.com/

international
--url https://www.justonecookbook.com/
--url https://www.maangchi.com/
--url https://www.chinasichuanfood.com/
--url https://omnivorescookbook.com/
--url https://www.indianhealthyrecipes.com/
--url https://hebbarskitchen.com/
--url https://www.archanaskitchen.com/
--url https://www.koreanbapsang.com/
--url https://hot-thai-kitchen.com/
--url https://www.recipetineats.com/

cocktails
--url https://www.liquor.com/
--url https://www.diffordsguide.com/
--url https://www.seriouseats.com/cocktails
--url https://www.acouplecooks.com/drinks/

--url https://www.allrecipes.com/ \
--url https://www.bbcgoodfood.com/ \
--url https://www.simplyrecipes.com/ \ 
--url https://www.seriouseats.com/ \
--url https://www.epicurious.com/ \
--url https://www.bonappetit.com/ \
--url https://www.thekitchn.com/ \
--url https://www.foodnetwork.com/ \ 
--url https://www.delish.com/ \
--url https://www.kingarthurbaking.com/ \
--url https://www.foodandwine.com/ \
--url https://www.saveur.com/ \
--url https://www.marthastewart.com/ \
--url https://www.tasteofhome.com/ \
--url https://www.myrecipes.com/ \ 
--url https://www.eatingwell.com/\
--url https://www.food.com/ \
--url https://www.yummly.com/ \

``` sh
quickbeam data crawl \
  --routes ~/fangorn/embeddings/examples/cc/routes_generic.json \
  --extractors ~/fangorn/embeddings/examples/cc/extractors/ \
  --url https://www.bbcgoodfood.com/recipes/ \
  --url https://www.simplyrecipes.com/recipes/ \
  --url https://www.seriouseats.com/ \
  --match-type prefix \
  --limit 200 \
  --out ~/fangorn/embeddings/examples/cc/recipes.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

---


Then we can test out the crawler service itself

```
quickbeam crawl --crawl-job-schema "test.crawl.job.0=0x3a313ac272bfaa668623c2af0ef4a77dfdccc92bc46cfe78235f5fbeef57c90f" \ 
--graph-api-key _ \
--ipfs-gateway ... \
```