"""GHL083-GHL088 — observability and operational safety.

Every rule here gets a workflow that trips it AND a correctly built workflow
that does not. These six all fire on the absence of something, which is the
easiest kind of check to write badly: "no timeout declared" and "no timeout in
the shape I happened to look at" produce the same finding, and only one of them
is true. So most of these tests are the negative ones.

The negatives that carry a `# was a false positive` comment are the ones an
adversarial pass actually found and the rule was narrowed for. They are not
hypothetical: each was a correct, ordinary build that the first version of this
pack reported as broken.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import CATEGORIES, RULES, run, run_all  # noqa: E402

MINE = ("GHL083", "GHL084", "GHL085", "GHL086", "GHL087", "GHL088")
HERE = os.path.dirname(os.path.abspath(__file__))
FRAGMENT = os.path.join(HERE, "..", "examples", "packs", "observability.json")


def bundle(workflows, custom_values=None, **extra):
    data = {"workflows": workflows, "customValues": custom_values or {}}
    data.update(extra)
    return data


def audit(workflows, custom_values=None, config=None, **extra):
    return run(Account.load(bundle(workflows, custom_values, **extra),
                            config=config))


def rules_hit(workflows, custom_values=None, config=None, **extra):
    return {f.rule for f in audit(workflows, custom_values, config, **extra)}


def findings_for(rule_id, workflows, custom_values=None, config=None, **extra):
    return [f for f in audit(workflows, custom_values, config, **extra)
            if f.rule == rule_id]


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello"):
    return {"type": "sms", "name": name, "meta": {"body": body}}


def wait(name="Wait"):
    return {"type": "wait", "name": name, "meta": {"delay": "10 minutes"}}


def webhook(name="Sync", url="https://api.example.com/sync", **meta):
    """A GoHighLevel-native webhook action."""
    m = {"url": url}
    m.update(meta)
    return {"type": "webhook", "name": name, "meta": m}


def http_node(name="Call the API", url="https://api.example.com/v1/notes",
              **params):
    """An n8n HTTP Request node, where timeout and retry are real settings."""
    p = {"method": "GET", "url": url}
    p.update(params)
    return {"type": "n8n-nodes-base.httpRequest", "name": name, "id": "step_1",
            "typeVersion": 4, "parameters": p}


def goto(name="Back to the call", target="step_1"):
    return {"type": "goto", "name": name, "id": "step_goto",
            "meta": {"targetStepId": target}}


def notify(user="usr_a", **meta):
    m = {"channel": "email", "recipientUserId": user,
         "message": "Something needs a look."}
    m.update(meta)
    return {"type": "internal_notification", "name": "Tell someone",
            "meta": m}


def tag_trigger(tag):
    return {"type": "contact_tag_added", "name": "Tag added",
            "filters": [{"tag": tag}]}


SECRET = "Zk29fJq4Lm81PdTr03Xa7Bw5"
BEARER = "Bearer 8f2c1d94ab7e4f0b93c6e5a2d7b41c60"


class CredentialRules(unittest.TestCase):
    """GHL083 — a key that is in the export is already outside the account."""

    def test_key_in_a_query_string_is_critical(self):
        step = webhook(url=f"https://api.example.com/v2/x?api_key={SECRET}")
        found = findings_for("GHL083", [wf("Push", [step])])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_bearer_token_in_a_header_is_high(self):
        step = webhook(headers=[{"name": "Authorization", "value": BEARER}])
        found = findings_for("GHL083", [wf("Push", [step])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_a_custom_value_reference_passes(self):
        step = webhook(
            url="https://api.example.com/v2/x?api_key={{custom_values.key}}",
            headers=[{"name": "Authorization",
                      "value": "Bearer {{ custom_values.warehouse_key }}"}])
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_label_under_a_secret_shaped_key_passes(self):
        """'tokenType: bearer' is a setting, not a credential."""
        step = webhook(tokenType="bearer", authType="header")
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_long_value_under_an_ordinary_key_passes(self):
        step = webhook(contactId="abc123def456ghi789jkl")
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_placeholder_is_left_to_ghl008(self):
        step = webhook(apiKey="REPLACE_WITH_YOUR_KEY_12345")
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_stored_credential_reference_passes(self):
        """was a false positive: {"credentialId": ...} is a pointer, not a key.

        Rotating a key nobody leaked is a wasted afternoon, and the client
        cannot disprove the claim from their side — which is exactly the kind
        of finding that makes the other 99 look like guesses.
        """
        step = webhook(credentialId="cred_9f2a1b8c7d6e5f4a",
                       secretName="warehouse_api_key_v2",
                       tokenId="550e8400-e29b-41d4-a716-446655440000")
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_the_n8n_credentials_block_passes(self):
        """was a false positive: n8n keeps the secret server-side."""
        step = http_node(options={"timeout": 5000})
        step["credentials"] = {"httpHeaderAuth": {"id": "kZ8Xq2Lm41PdTr09",
                                                  "name": "Warehouse Auth"}}
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_presigned_download_link_passes(self):
        """was a false positive: an S3 signature is scoped and expires itself."""
        step = sms("Send the file")
        step["meta"] = {"body": "Your report: https://files.example.com/r.pdf"
                                "?X-Amz-Credential=AKIA9K2LM81PDTR03XA7"
                                "&X-Amz-Expires=3600&X-Amz-Signature=7bf1c0d9"}
        self.assertNotIn("GHL083", rules_hit([wf("Delivery", [step])]))

    def test_an_x_api_key_header_still_fires(self):
        """The reference filter must not swallow the real thing."""
        step = webhook(headers=[{"name": "X-Api-Key", "value": SECRET}])
        self.assertIn("GHL083", rules_hit([wf("Push", [step])]))

    def test_a_draft_leaks_a_key_exactly_as_well_as_a_live_one(self):
        step = webhook(url=f"https://api.example.com/v2/x?api_key={SECRET}")
        hits = rules_hit([wf("Push", [step], status="draft")])
        self.assertIn("GHL083", hits)

    def test_the_finding_never_reprints_the_key(self):
        step = webhook(url=f"https://api.example.com/v2/x?api_key={SECRET}")
        found = findings_for("GHL083", [wf("Push", [step])])
        blob = json.dumps([f.to_dict() for f in found])
        self.assertNotIn(SECRET, blob)
        self.assertIn("(24 chars)", blob)

    def test_a_url_with_no_credential_in_it_passes(self):
        step = webhook(url="https://api.example.com/v2/x?contact_id=99123456")
        self.assertNotIn("GHL083", rules_hit([wf("Push", [step])]))


class TimeoutRules(unittest.TestCase):
    """GHL084 — checked only where a timeout is a settable field."""

    def test_http_node_with_no_timeout_is_flagged(self):
        self.assertIn("GHL084", rules_hit([wf("Sync", [http_node()])]))

    def test_a_timeout_in_options_passes(self):
        step = http_node(options={"timeout": 10000})
        self.assertNotIn("GHL084", rules_hit([wf("Sync", [step])]))

    def test_a_timeout_written_as_a_string_passes(self):
        step = http_node(options={"timeout": "10000"})
        self.assertNotIn("GHL084", rules_hit([wf("Sync", [step])]))

    def test_a_zero_timeout_is_not_a_timeout(self):
        step = http_node(options={"timeout": 0})
        self.assertIn("GHL084", rules_hit([wf("Sync", [step])]))

    def test_a_ghl_webhook_action_is_not_held_to_an_n8n_setting(self):
        self.assertNotIn("GHL084", rules_hit([wf("Sync", [webhook()])]))

    def test_the_ghl_http_request_action_is_not_an_n8n_node(self):
        """was a false positive: GHL's own action normalises to 'httprequest'.

        It has no timeout control in the builder, so the fix — Options ->
        Timeout — is a menu the reader does not have.
        """
        step = {"type": "http_request", "name": "Call the API",
                "meta": {"method": "GET", "url": "https://api.example.com/v1"}}
        self.assertNotIn("GHL084", rules_hit([wf("Sync", [step])]))

    def test_a_workflow_level_execution_timeout_passes(self):
        """was a false positive: n8n's Timeout Workflow After bounds the hang."""
        w = wf("Sync", [http_node()], settings={"executionTimeout": 120})
        self.assertNotIn("GHL084", rules_hit([w]))

    def test_a_bare_http_request_node_with_a_typeversion_is_still_n8n(self):
        step = {"type": "httpRequest", "name": "Call", "typeVersion": 4,
                "parameters": {"method": "GET", "url": "https://api.x.com/v1"}}
        self.assertIn("GHL084", rules_hit([wf("Sync", [step])]))

    def test_a_message_step_is_left_alone(self):
        self.assertNotIn("GHL084", rules_hit([wf("Nurture", [sms()])]))

    def test_draft_workflows_are_not_checked(self):
        hits = rules_hit([wf("Sync", [http_node()], status="draft")])
        self.assertNotIn("GHL084", hits)

    def test_a_status_less_export_is_still_checked(self):
        """n8n bundles carry no publish state — absent must not mean exempt."""
        raw = {"name": "Sync", "steps": [http_node()]}
        self.assertIn("GHL084", rules_hit([raw]))


class IdempotencyRules(unittest.TestCase):
    """GHL085 — only where the same POST can genuinely run twice."""

    def loop(self, **params):
        step = http_node(method="POST", **params)
        return wf("Order Sync", [step, wait(), goto()])

    def test_post_inside_a_goto_loop_is_flagged(self):
        self.assertIn("GHL085", rules_hit([self.loop()]))

    def test_an_idempotency_header_passes(self):
        w = self.loop(headers={"Idempotency-Key": "{{ contact.id }}"})
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_the_n8n_name_value_header_shape_is_read(self):
        w = self.loop(headerParameters={"parameters": [
            {"name": "Idempotency-Key", "value": "{{ contact.id }}"}]})
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_get_is_safe_to_repeat(self):
        w = wf("Order Sync", [http_node(), wait(), goto()])
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_post_that_can_only_run_once_passes(self):
        w = wf("Order Sync", [http_node(method="POST")])
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_goto_that_jumps_forward_is_not_a_loop(self):
        """was a false positive: Go-To skips ahead as often as it loops back.

        The finding says the workflow loops back to this step. If the jump
        lands past it, that sentence is simply untrue.
        """
        w = wf("Order Intake", [http_node(method="POST"), goto(target="thanks"),
                                dict(sms("Upsell"), id="upsell"),
                                dict(sms("Thanks"), id="thanks")])
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_goto_whose_target_does_not_resolve_is_left_to_ghl020(self):
        w = wf("Order Sync", [http_node(method="POST"), goto(target="gone")])
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_parent_key_pointing_at_the_step_above_is_not_a_jump(self):
        """A branch child carries its parent's id. That is wiring, not a loop."""
        step = http_node(method="POST")
        jump = {"type": "goto", "name": "Go to the next stage", "id": "g1",
                "parentKey": "step_1", "meta": {"targetStepId": "later"}}
        w = wf("Order Sync", [step, jump, dict(sms("Done"), id="later")])
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_retries_declared_on_the_step_count_as_a_retry_path(self):
        step = http_node(method="POST", options={"timeout": 5000})
        step["retryOnFail"] = True
        step["maxTries"] = 3
        self.assertIn("GHL085", rules_hit([wf("Order Sync", [step])]))

    def test_a_left_over_max_tries_with_retries_off_passes(self):
        """was a false positive: n8n keeps maxTries after Retry On Fail is off."""
        step = http_node(method="POST", options={"timeout": 5000})
        step["retryOnFail"] = False
        step["maxTries"] = 3
        self.assertNotIn("GHL085", rules_hit([wf("Order Sync", [step])]))

    def test_an_upsert_endpoint_dedupes_on_its_own_side(self):
        """was a false positive: an upsert IS the receiver-side idempotency."""
        w = self.loop(url="https://services.leadconnectorhq.com/contacts/upsert")
        self.assertNotIn("GHL085", rules_hit([w]))

    def test_a_money_endpoint_is_high_and_anything_else_is_medium(self):
        money = self.loop(url="https://api.example.com/v1/charges")
        self.assertEqual([f.severity for f in findings_for("GHL085", [money])],
                         ["high"])
        other = self.loop(url="https://api.example.com/v1/notes")
        self.assertEqual([f.severity for f in findings_for("GHL085", [other])],
                         ["medium"])

    def test_an_inbound_webhook_node_is_not_an_outbound_post(self):
        """n8n's Webhook TRIGGER declares an HTTP method and no destination."""
        trigger_node = {"type": "n8n-nodes-base.webhook", "name": "Order paid",
                        "typeVersion": 2, "id": "step_1",
                        "parameters": {"httpMethod": "POST", "path": "orders"}}
        w = wf("Order Intake", [trigger_node, wait(), goto()])
        self.assertNotIn("GHL085", rules_hit([w]))


class DestructiveTrailRules(unittest.TestCase):
    """GHL086 — the question that comes after 'did the call work?'."""

    DELETE = webhook(name="Remove them from the warehouse",
                     url="https://api.example.com/v2/people/1",
                     method="DELETE")

    def test_a_delete_with_nothing_recording_it_is_flagged(self):
        self.assertIn("GHL086", rules_hit([wf("Erase", [self.DELETE])]))

    def test_a_note_step_clears_it(self):
        steps = [{"type": "add_note", "name": "Log what we removed",
                  "meta": {"body": "Removed record 1"}}, self.DELETE]
        self.assertNotIn("GHL086", rules_hit([wf("Erase", steps)]))

    def test_an_internal_notification_clears_it(self):
        self.assertNotIn("GHL086",
                         rules_hit([wf("Erase", [notify(), self.DELETE])]))

    def test_a_tag_clears_it(self):
        steps = [{"type": "add_contact_tag", "name": "Mark it",
                  "meta": {"tags": ["warehouse-erased"]}}, self.DELETE]
        self.assertNotIn("GHL086", rules_hit([wf("Erase", steps)]))

    def test_a_log_sheet_call_clears_it(self):
        steps = [self.DELETE,
                 webhook(name="Append to the audit sheet",
                         url="https://sheets.example.com/append")]
        self.assertNotIn("GHL086", rules_hit([wf("Erase", steps)]))

    def test_a_google_sheets_node_clears_it(self):
        """was a false positive: the sheet append IS the audit trail.

        It is not an HTTP call and its name need not say 'log', so the first
        version of this rule could not see the most common trail there is.
        """
        steps = [{"type": "n8n-nodes-base.googleSheets", "name": "Append a row",
                  "typeVersion": 4,
                  "parameters": {"operation": "append", "documentId": "doc_1"}},
                 self.DELETE]
        self.assertNotIn("GHL086", rules_hit([wf("Cleanup", steps)]))

    def test_a_step_that_records_the_deletion_is_not_itself_a_deletion(self):
        """was a false positive: "Write the erase to the ledger" is the trail."""
        ledger = webhook(name="Write the erase to the ledger",
                         url="https://api.example.com/v1/ledger",
                         method="POST")
        found = findings_for("GHL086", [wf("Erase", [ledger, self.DELETE])])
        self.assertEqual([f.step for f in found], [])

    def test_an_n8n_delete_operation_is_flagged(self):
        step = {"type": "n8n-nodes-base.googleSheets", "name": "Tidy the sheet",
                "typeVersion": 4,
                "parameters": {"operation": "delete", "documentId": "doc_1"}}
        self.assertIn("GHL086", rules_hit([wf("Cleanup", [step])]))

    def test_a_notification_about_a_refund_is_not_itself_destructive(self):
        step = notify()
        step["name"] = "Tell the team about the refund"
        self.assertNotIn("GHL086", rules_hit([wf("Refund Alert", [step])]))

    def test_an_ordinary_post_is_not_destructive(self):
        step = webhook(name="Create the customer", method="POST")
        self.assertNotIn("GHL086", rules_hit([wf("Sync", [step])]))

    def test_the_title_reads_as_english(self):
        """'a erase call' shipped in the first version of this rule."""
        step = webhook(name="Erase the record",
                       url="https://api.example.com/v2/erase", method="POST")
        found = findings_for("GHL086", [wf("Erase", [step])])
        self.assertIn("an erase call", found[0].title)

    def test_draft_workflows_are_not_checked(self):
        hits = rules_hit([wf("Erase", [self.DELETE], status="draft")])
        self.assertNotIn("GHL086", hits)


class AlertBusFactorRules(unittest.TestCase):
    """GHL087 — one addressee is not a rota."""

    def alerts(self, count, step=None, prefix="Alert"):
        return [wf(f"{prefix} {i}", [step or notify()]) for i in range(count)]

    def test_three_workflows_to_one_person_is_high(self):
        found = findings_for("GHL087", self.alerts(3))
        self.assertEqual([f.severity for f in found], ["high"])

    def test_two_workflows_are_one_person_owning_one_area(self):
        """was a false positive: the rule fired a step below its own threshold.

        Its docstring argued for three and the code shipped at two, so the
        most defensible half of the rule was the half nobody had run.
        """
        self.assertNotIn("GHL087", rules_hit(self.alerts(2)))

    def test_one_workflow_is_not_a_pattern(self):
        self.assertNotIn("GHL087", rules_hit(self.alerts(1)))

    def test_alerting_that_exists_elsewhere_drops_it_to_medium(self):
        """was a false positive: 'the whole monitoring layer' was overclaimed.

        An account with a Slack channel on its other alerts has a second pair
        of eyes; these three workflows are still a thin spot, not an outage.
        """
        flows = self.alerts(3) + [wf("Ops", [{"type": "slack",
                                              "name": "Post to ops",
                                              "meta": {"channelId": "C0123"}}])]
        found = findings_for("GHL087", flows)
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_an_ops_channel_node_counts_as_alerting_elsewhere(self):
        flows = self.alerts(3) + [wf("Ops", [{
            "type": "n8n-nodes-base.telegram", "name": "Ping the ops chat",
            "typeVersion": 1, "parameters": {"chatId": "-100123"}}])]
        found = findings_for("GHL087", flows)
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_a_shared_mailbox_is_not_one_person(self):
        """was a false positive: ops@ is a room, not a bus factor of one."""
        step = {"type": "internal_notification", "name": "Tell the team",
                "meta": {"to": "ops@acme.com"}}
        self.assertNotIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_a_named_persons_address_is_still_one_person(self):
        step = {"type": "internal_notification", "name": "Tell Dana",
                "meta": {"to": "dana.reyes@acme.com"}}
        self.assertIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_the_delivery_channel_is_not_a_destination(self):
        """GoHighLevel writes {"channel": "email"} to mean 'send it by email'."""
        self.assertIn("GHL087", rules_hit(self.alerts(3, notify())))

    def test_two_named_recipients_is_a_rota(self):
        step = {"type": "internal_notification", "name": "Tell someone",
                "meta": {"userIds": ["usr_a", "usr_b"]}}
        self.assertNotIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_two_addresses_in_one_field_are_two_people(self):
        step = {"type": "internal_notification", "name": "Tell the team",
                "meta": {"to": "dana@acme.com, sam@acme.com"}}
        self.assertNotIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_a_slack_channel_is_not_one_person(self):
        step = {"type": "slack", "name": "Post to ops",
                "meta": {"channelId": "C0123", "message": "look at this"}}
        self.assertNotIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_the_assigned_user_merge_field_is_not_one_person(self):
        step = {"type": "internal_notification", "name": "Tell the owner",
                "meta": {"to": "{{ contact.assigned_to.email }}"}}
        self.assertNotIn("GHL087", rules_hit(self.alerts(3, step)))

    def test_different_people_across_workflows_pass(self):
        flows = [wf(f"Alert {i}", [notify(user=f"usr_{i}")]) for i in range(3)]
        self.assertNotIn("GHL087", rules_hit(flows))

    def test_workflows_with_no_alerts_are_not_counted(self):
        self.assertNotIn("GHL087", rules_hit([wf("Nurture", [sms(), sms()])]))

    def test_the_persons_name_is_resolved_when_the_account_supplies_it(self):
        users = [{"id": "usr_a", "name": "Morgan Ellis", "active": True}]
        found = findings_for("GHL087", self.alerts(3), users=users)
        self.assertIn("Morgan Ellis", found[0].title)


class SilentErrorPathRules(unittest.TestCase):
    """GHL088 — a tag is a record, not an alarm."""

    def failing_sync(self, name="CRM Sync", tag="err:sync-failed"):
        steps = [webhook(saveResponse=True),
                 {"type": "if_else", "name": "Sync failed?",
                  "meta": {"conditions": [{"field": "response.status"}]}},
                 {"type": "add_contact_tag", "name": "Mark it broken",
                  "meta": {"tags": [tag]}}]
        return wf(name, steps)

    def listener(self, tag):
        return wf("Ops Alerting", [notify()], [tag_trigger(tag)])

    def test_an_error_path_that_notifies_nobody_is_flagged(self):
        found = findings_for("GHL088", [self.failing_sync()])
        self.assertEqual([f.severity for f in found], ["high"])
        self.assertIn("tells nobody", found[0].title)

    def test_a_notification_in_the_workflow_clears_it(self):
        w = self.failing_sync()
        w["steps"].append(notify())
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_a_slack_call_in_the_workflow_clears_it(self):
        w = self.failing_sync()
        w["steps"].append(webhook(name="Post the failure to Slack",
                                  url="https://hooks.slack.com/services/T/B/x"))
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_a_telegram_node_clears_it(self):
        """was a false positive: an n8n build alerts through a channel node."""
        w = self.failing_sync()
        w["steps"].append({"type": "n8n-nodes-base.telegram",
                           "name": "Ping the on-call chat", "typeVersion": 1,
                           "parameters": {"operation": "sendMessage",
                                          "chatId": "-100123"}})
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_a_telegram_bot_call_clears_it(self):
        """was a false positive: the same alarm written as an HTTP call."""
        w = self.failing_sync()
        w["steps"].append(webhook(
            name="Ping the ops chat",
            url="https://api.telegram.org/bot123/sendMessage", method="POST"))
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_a_task_assigned_to_a_person_clears_it(self):
        """was a false positive: a task lands in that person's list."""
        w = self.failing_sync()
        w["steps"].append({"type": "add_task", "name": "Fix the CRM sync",
                           "meta": {"assignedTo": "usr_a",
                                    "title": "Sync is down"}})
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_an_email_to_a_fixed_address_clears_it(self):
        """was a false positive: a hardcoded address is a colleague's desk."""
        w = self.failing_sync()
        w["steps"].append({"type": "email", "name": "Email the developer",
                           "meta": {"to": "dev@acme.com",
                                    "subject": "Sync failed"}})
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_an_email_to_the_contact_does_not_clear_it(self):
        """The recipient that makes it an alarm is the one that is not a token."""
        w = self.failing_sync()
        w["steps"].append({"type": "email", "name": "Apologise",
                           "meta": {"to": "{{ contact.email }}",
                                    "subject": "Sorry about that"}})
        self.assertIn("GHL088", rules_hit([w]))

    def test_an_attached_error_workflow_clears_it(self):
        """was a false positive: GHL043 recommends exactly this arrangement."""
        w = self.failing_sync()
        w["settings"] = {"errorWorkflow": "wf_error_handler"}
        self.assertNotIn("GHL088", rules_hit([w]))

    def test_a_listener_watching_that_tag_clears_it(self):
        flows = [self.failing_sync(), self.listener("err:sync-failed")]
        self.assertNotIn("GHL088", rules_hit(flows))

    def test_a_listener_watching_a_different_tag_is_the_sharper_finding(self):
        flows = [self.failing_sync(), self.listener("err:import-stalled")]
        found = findings_for("GHL088", flows)
        self.assertEqual(len(found), 1)
        self.assertIn("not one any alert listens for", found[0].title)

    def test_a_listener_on_an_unrelated_tag_is_not_an_error_alerting_layer(self):
        """'your error tag is not on the list' needs a list to point at.

        A hot-lead alert is alerting, but it is not the failure listener this
        variant tells the reader to go and edit.
        """
        flows = [self.failing_sync(), self.listener("hot-lead")]
        found = findings_for("GHL088", flows)
        self.assertIn("tells nobody", found[0].title)

    def test_a_workflow_with_no_failure_handling_is_left_to_ghl041(self):
        steps = [webhook(saveResponse=True),
                 {"type": "if_else", "name": "Did it come back OK?",
                  "meta": {"conditions": [{"field": "response.status"}]}}]
        self.assertNotIn("GHL088", rules_hit([wf("CRM Sync", steps)]))

    def test_a_dunning_sequence_is_not_an_integration_failure(self):
        steps = [{"type": "if_else", "name": "Payment failed?", "meta": {}},
                 sms("Card declined", "Your card was declined.")]
        self.assertNotIn("GHL088", rules_hit([wf("Dunning", steps)]))

    def test_draft_workflows_are_not_checked(self):
        w = self.failing_sync()
        w["status"] = "draft"
        self.assertNotIn("GHL088", rules_hit([w]))


class NoDoubleReporting(unittest.TestCase):
    """One defect, one finding. Two rules on one step reads as padding."""

    def test_a_delete_is_reported_once_not_once_per_lookalike_step(self):
        steps = [webhook(name="Delete the record",
                         url="https://api.example.com/v2/people/1",
                         method="DELETE"),
                 webhook(name="Purge the cache",
                         url="https://api.example.com/v2/cache",
                         method="DELETE")]
        found = findings_for("GHL086", [wf("Cleanup", steps)])
        # The second delete is not a trail for the first, and vice versa —
        # but neither is reported twice.
        self.assertEqual(len(found), 2)
        self.assertEqual(len({f.step for f in found}), 2)

    def test_an_alert_about_a_deletion_trips_neither_086_nor_088(self):
        step = {"type": "slack", "name": "Post the delete to ops",
                "meta": {"channelId": "C0123",
                         "message": "We deleted a record."}}
        hits = rules_hit([wf("Erase Alert", [step])])
        self.assertNotIn("GHL086", hits)
        self.assertNotIn("GHL088", hits)

    def test_a_credential_in_a_url_is_083_alone_in_this_pack(self):
        step = http_node(url=f"https://api.example.com/v2/x?api_key={SECRET}",
                         options={"timeout": 5000})
        mine = rules_hit([wf("Push", [step])]) & set(MINE)
        self.assertEqual(mine, {"GHL083"})


class PackCatalog(unittest.TestCase):
    def test_all_six_are_registered_once(self):
        ids = [r.id for r in RULES if r.id in MINE]
        self.assertEqual(sorted(ids), list(MINE))

    def test_each_declares_a_valid_severity_and_category(self):
        for r in RULES:
            if r.id in MINE:
                self.assertIn(r.severity, ("critical", "high", "medium", "low"))
                self.assertIn(r.category, CATEGORIES)


class Fragment(unittest.TestCase):
    """The shipped example fragment has to demo all six, and skip none."""

    def setUp(self):
        with open(FRAGMENT) as fh:
            self.acct = Account.load(json.load(fh))
        self.findings, self.skips = run_all(self.acct)

    def test_every_rule_in_this_pack_fires_on_it(self):
        tripped = {f.rule for f in self.findings}
        self.assertEqual(sorted(set(MINE) - tripped), [])

    def test_none_of_this_packs_rules_skips_on_it(self):
        self.assertEqual([s.rule for s in self.skips if s.rule in MINE], [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.findings
                if f.rule in MINE and not f.cost.strip()]
        self.assertEqual(bare, [])

    def test_no_finding_leaks_the_demo_key(self):
        blob = json.dumps([f.to_dict() for f in self.findings])
        self.assertNotIn("Zk29fJq4Lm81PdTr03Xa7Bw5", blob)


class Robustness(unittest.TestCase):
    """A malformed export must produce a finding or nothing, never a traceback.

    Every shape here has been seen in a real export or is one line away from
    one. A crash in any rule stops the other 99 from running, so this is the
    test that protects the rest of the catalog from this pack.
    """

    CASES = [
        [],
        {},
        [{"name": "x", "status": "published", "steps": None, "triggers": None,
          "settings": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "steps": [{"type": "webhook",
                                                         "meta": ["a", "list"]}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "webhook", "meta": {"url": ["not", "a", "string"],
                                                 "method": 7,
                                                 "headers": None}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "internal_notification",
                     "meta": {"recipientUserId": {"nested": "dict"}}}]}],
        [{"_id": 12345, "name": 999, "status": True,
          "steps": [{"type": 7, "name": None}]}],
        # A settings value that is a string, which the timeout and error
        # workflow reads both walk.
        [{"name": "x", "status": "published", "settings": "windowed",
          "steps": [http_node()]}],
        # A trigger that is a list, and a tag block that is not strings.
        [{"name": "x", "status": "published", "triggers": [["a", "b"]],
          "steps": [{"type": "add_contact_tag", "meta": {"tags": [1, None]}}]}],
        # A Go-To with no target, alongside a POST — the loop resolver.
        [{"name": "x", "status": "published",
          "steps": [{"type": "webhook", "id": None,
                     "meta": {"method": "POST", "url": "https://a.example/x"}},
                    {"type": "goto"}]}],
        # Recipients as numbers and nested lists.
        [{"name": "x", "status": "published",
          "steps": [{"type": "internal_notification",
                     "meta": {"to": 4155550101, "userIds": [["a"], {"b": 1}]}}]}],
        # A step whose settings block is a string, and a URL that is a bool.
        [{"name": "x", "status": "published",
          "steps": [{"type": "n8n-nodes-base.httpRequest", "typeVersion": 4,
                     "parameters": "GET https://a.example", "url": True}]}],
        {"workflows": [], "users": "dana"},
        {"workflows": [], "users": [{"id": "usr_a"}, "sam", 7]},
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            findings, skips = run_all(Account.load(data), only=MINE)
            self.assertIsInstance(findings, list)
            self.assertIsInstance(skips, list)

    def test_every_finding_from_a_malformed_export_still_reads(self):
        """A finding built from junk must still be a sentence with a cost."""
        for data in self.CASES:
            for f in run_all(Account.load(data), only=MINE)[0]:
                self.assertTrue(f.title.strip())
                self.assertTrue(f.symptom.strip())
                self.assertTrue(f.fix.strip())
                self.assertTrue(f.cost.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
