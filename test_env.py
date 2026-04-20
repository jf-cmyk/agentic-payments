import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class BlocksizeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    api_key: str = Field(..., alias="BLOCKSIZE_API_KEY")

os.environ["BLOCKSIZE_API_KEY"] = "TestKey"
settings = BlocksizeSettings()
print(settings.api_key)
