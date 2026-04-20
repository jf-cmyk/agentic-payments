import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class BlocksizeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    api_key: str = Field(..., alias="BLOCKSIZE_API_KEY")

def _find_dotenv() -> str | None:
    return None

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")
    blocksize: BlocksizeSettings

    def __init__(self, **kwargs):
        dotenv_path = _find_dotenv()
        env_kwargs = {"_env_file": dotenv_path} if dotenv_path else {}
        super().__init__(blocksize=BlocksizeSettings(**env_kwargs))

os.environ["BLOCKSIZE_API_KEY"] = "TestKey3"
try:
    settings = Settings()
    print("Success:", settings.blocksize.api_key)
except Exception as e:
    print("Error:", repr(e))
