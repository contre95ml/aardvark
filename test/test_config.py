import yaml

import confuse
import pytest

import aardvark.configuration
from aardvark.configuration import create_config


@pytest.fixture(scope="function")
def temp_config_file(tmpdir_factory):
    config_path = tmpdir_factory.mktemp("aardvark").join("config.yaml")
    return str(config_path)


@pytest.fixture(scope="function", autouse=True)
def mock_config(monkeypatch):
    # Monkeypatch the actual config object so we don't poison it for future tests
    monkeypatch.setattr(
        aardvark.configuration, "CONFIG", confuse.Configuration("aardvark")
    )


def test_create_config(temp_config_file):
    create_config(
        aardvark_role="role",
        swag_bucket="bucket",
        swag_filter="filter",
        swag_service_enabled_requirement="service",
        arn_partition="aws",
        sqlalchemy_database_uri="sqlite://////////////hi.db",
        sqlalchemy_track_modifications=True,
        num_threads=99,
        region="us-underground-5",
        filename=temp_config_file,
    )

    with open(temp_config_file, "r") as f:
        file_data = yaml.safe_load(f)

    assert file_data["aws"]["rolename"] == "role"
    assert file_data["aws"]["region"] == "us-underground-5"
    assert file_data["aws"]["arn_partition"] == "aws"
    assert file_data["swag"]["bucket"] == "bucket"
    assert file_data["swag"]["filter"] == "filter"
    assert file_data["swag"]["service_enabled_requirement"] == "service"
    assert file_data["updater"]["num_threads"] == 99
    assert file_data["sqlalchemy"]["database_uri"] == "sqlite://////////////hi.db"
    assert file_data["sqlalchemy"]["track_modifications"]
