import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class BlocksizeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    api_key: str = Field(..., alias="BLOCKSIZE_API_KEY")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")
    blocksize: BlocksizeSettings

    def __init__(self, **kwargs):
        super().__init__(blocksize=BlocksizeSettings(**kwargs))

os.environ["BLOCKSIZE_API_KEY"] = "TestKey2"
try:
    settings = Settings()
    print("Success:", settings.blocksize.api_key)
except Exception as e:
    print("Error:", repr(e))
