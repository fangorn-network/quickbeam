quickbeam data crawl \
  --routes ~/fangorn/embeddings/examples/cc/routes_recipes.json \
  --extractors ~/fangorn/embeddings/examples/cc/extractors/ \
  --url https://www.bbcgoodfood.com/recipes/ \
  --url https://www.kingarthurbaking.com/recipes/ \
  --url https://sallysbakingaddiction.com/ \
  --url https://preppykitchen.com/ \
  --url https://www.handletheheat.com/ \
  --url https://sugarspunrun.com/ \
  --url https://www.browneyedbaker.com/ \
  --url https://www.livewellbakeoften.com/ \
  --url https://www.joyofbaking.com/ \
  --url https://www.baking-sense.com/ \
  --url https://www.davidlebovitz.com/ \
  --url https://pinchofyum.com/ \
  --url https://damndelicious.net/ \
  --url https://www.acouplecooks.com/ \
  --url https://www.feastingathome.com/ \
  --url https://www.spendwithpennies.com/ \
  --url https://www.themediterraneandish.com/ \
  --url https://www.koreanbapsang.com/ \
  --url https://mykoreankitchen.com/ \
  --url https://omnivorescookbook.com/ \
  --url https://www.indianhealthyrecipes.com/ \
  --url https://hebbarskitchen.com/ \
  --url https://www.vegrecipesofindia.com/ \
  --url https://www.isabeleats.com/ \
  --url https://amazingribs.com/ \
  --url https://girlscangrill.com/ \
  --url https://www.vindulge.com/ \
  --url https://www.diffordsguide.com/cocktails/ \
  --url https://imbibemagazine.com/ \
  --url https://www.budgetbytes.com/ \
  --match-type prefix \
  --limit 1500 \
  --n-proc 8 \
  --extract-timeout 3600 \
  --out ~/fangorn/embeddings/examples/cc/recipes.json \
  --aggregator free \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon


  # --url https://www.meatchurch.com/blogs/recipes \
    # --url https://www.theeducatedbarfly.com/ \
  # --url https://www.cocktailcontessa.com/ \

  # quickbeam data crawl \
  # --routes ~/fangorn/embeddings/examples/cc/routes_generic.json \
  # --extractors ~/fangorn/embeddings/examples/cc/extractors/ \
  # --url https://www.bbcgoodfood.com/recipes/ \
  # --match-type prefix \
  # --limit 1000000 \
  # --out ~/fangorn/embeddings/examples/cc/recipes.json \
  # --aggregator free \
  # --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon



  
  