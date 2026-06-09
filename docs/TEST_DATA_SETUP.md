# Test Data Setup

install and config the aws cli

``` sh
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
```

``` sh
# follow the prompts
# AWS access key id = r2 access key id
aws configure --profile r2
# verify 
aws --profile r2 \
  --endpoint-url https://2e0a41ed3fec719d427dfae6512efddf.r2.cloudflarestorage.com \
  s3 ls s3://my-first-dir/
```

We use the internet archive to fetch a bunch of audio for free.  

``` sh
pip install internetarchive

# download a collection 
ia search "collection:lemon-tunes" --itemlist > lemon-tunes-items.txt
ia download --itemlist lemon-tunes-items.txt --destdir ./ia-data --format="VBR MP3"
```

Now, we're ready to register the data in Fangorn. This should give us a baseline for cost (in terms of gas). We need to loop over the directory and register each item one-by-one until we can support arrays.