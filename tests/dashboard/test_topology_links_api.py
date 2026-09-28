"""Tests for manual TopologyLink CRUD API (apps/dashboard/topology_links_api.py)."""
import json

import pytest
from django.urls import reverse

from apps.devices.models import TopologyLink
from tests.conftest import CiscoSNMPDeviceFactory


@pytest.mark.django_db
class TestUpdateLinkKeepsOldOnValidationFailure:
    """Regression: _update_link từng xoá link cũ NGAY rồi mới validate body mới — request
    sai (vd thiếu remote_device) làm mất link cũ mà không tạo được link thay thế. Fix bọc
    trong transaction.atomic() + rollback khi _create_link trả lỗi.
    """

    def test_update_with_invalid_body_keeps_existing_link(self, logged_in_client):
        sw1 = CiscoSNMPDeviceFactory(name="SW1")
        sw2 = CiscoSNMPDeviceFactory(name="SW2")
        link = TopologyLink.objects.create(
            local_device=sw1,
            local_port="Gi0/1",
            link_kind="switch",
            remote_device=sw2,
            match_method="manual",
        )

        # Body thiếu "remote_device" (bắt buộc với kind=switch) → _create_link phải _bad().
        response = logged_in_client.post(
            reverse("dashboard:topology_link_detail", args=[link.pk]),
            data=json.dumps({
                "kind": "switch",
                "local_device": sw1.pk,
                "local_port": "Gi0/1",
                # remote_device cố ý thiếu
            }),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False

        # Link cũ PHẢI còn nguyên — không bị mất do request lỗi.
        assert TopologyLink.objects.filter(pk=link.pk).exists()
        link.refresh_from_db()
        assert link.remote_device_id == sw2.pk

    def test_update_with_valid_body_replaces_link(self, logged_in_client):
        sw1 = CiscoSNMPDeviceFactory(name="SW1")
        sw2 = CiscoSNMPDeviceFactory(name="SW2")
        sw3 = CiscoSNMPDeviceFactory(name="SW3")
        link = TopologyLink.objects.create(
            local_device=sw1,
            local_port="Gi0/1",
            link_kind="switch",
            remote_device=sw2,
            match_method="manual",
        )

        response = logged_in_client.post(
            reverse("dashboard:topology_link_detail", args=[link.pk]),
            data=json.dumps({
                "kind": "switch",
                "local_device": sw1.pk,
                "local_port": "Gi0/1",
                "remote_device": sw3.pk,
            }),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        new_link = TopologyLink.objects.get(
            local_device=sw1, local_port="Gi0/1", match_method="manual"
        )
        assert new_link.remote_device_id == sw3.pk
