from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from enums import DeploymentEdition
from services.entities.feature_entities import SystemFeatureModel
from services.feature_service import FeatureService


def test_system_feature_model_requires_deployment_edition() -> None:
    with pytest.raises(ValidationError):
        SystemFeatureModel.model_validate({})


@pytest.mark.parametrize(
    "edition",
    [
        DeploymentEdition.COMMUNITY,
        DeploymentEdition.ENTERPRISE,
        DeploymentEdition.CLOUD,
    ],
)
def test_get_system_features_uses_configured_deployment_edition(
    monkeypatch: pytest.MonkeyPatch,
    config_overrides: Callable[..., None],
    edition: DeploymentEdition,
) -> None:
    fulfill_from_enterprise = MagicMock()
    config_overrides(DEPLOYMENT_EDITION=edition)
    monkeypatch.setattr(
        "services.feature_service.FeatureService._fulfill_params_from_enterprise",
        fulfill_from_enterprise,
    )

    result = FeatureService.get_system_features()

    assert result.deployment_edition is edition
    assert result.model_dump(mode="json")["deployment_edition"] == edition.value
    if edition is DeploymentEdition.ENTERPRISE:
        fulfill_from_enterprise.assert_called_once_with(result)
    else:
        fulfill_from_enterprise.assert_not_called()
