from bs4 import BeautifulSoup
from cmoncrawl.processor.pipeline.extractor import BaseExtractor
from cmoncrawl.common.types import PipeMetadata

class MySimpleExtractor(BaseExtractor):
    def extract_soup(self, soup: BeautifulSoup, metadata: PipeMetadata):
        # The capture URL lives on the DomainRecord, not on PipeMetadata directly.
        url = metadata.domain_record.url

        if not soup:
            return None

        title = soup.title.string.strip() if soup.title and soup.title.string else "No Title"

        return {
            "title": title,
            "url": url,
        }

extractor = MySimpleExtractor()