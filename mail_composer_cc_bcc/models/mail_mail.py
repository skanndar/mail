# Copyright 2023 Camptocamp SA
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).


import os

from odoo import fields, models, tools

from odoo.addons.base.models.ir_mail_server import extract_rfc2822_addresses


def format_emails(partners):
    return [tools.formataddr((p.name or "", p.email)) for p in partners if p.email]


def format_emails_raw(partners):
    return [p.email for p in partners if p.email]


def format_emails_str(partners):
    emails = format_emails(partners)
    return ", ".join(emails)


class MailMail(models.Model):
    _inherit = "mail.mail"

    email_bcc = fields.Char("Bcc", help="Blind Cc message recipients")
    is_composer_cc_bcc = fields.Boolean(
        "Composer Cc/Bcc handling",
        copy=False,
        help="Set by the composer when this email was built with the Cc / Bcc "
        "handling of this module. It has to be stored: the handling must not "
        "depend on the `is_from_composer` context key, which only exists while "
        "the composer is sending. Any later send of the same mail.mail -- the "
        "standard Retry button (Settings > Technical > Emails), the queue cron, "
        "a resend wizard -- runs without it.",
    )

    def _expose_bcc_marker(self):
        """Whether to also add the informational ``X-Odoo-Bcc`` marker header.

        Disabled by default: unlike ``Bcc``, the marker is not stripped before
        sending, so it reaches the Bcc recipient. Enable it through the
        ``expose_x_odoo_bcc`` context key or the ``EXPOSE_X_ODOO_BCC``
        environment variable. Ported from OCA/mail#233 (18.0).
        """
        if self.env.context.get("expose_x_odoo_bcc"):
            return True
        return tools.str2bool(os.environ.get("EXPOSE_X_ODOO_BCC") or "", False)

    def _prepare_outgoing_list(self, mail_server=False, doc_to_followers=None):
        # First, return if we're not coming from the Mail Composer
        res = super()._prepare_outgoing_list(
            mail_server=mail_server, doc_to_followers=doc_to_followers
        )
        # Read the marker from the record, not from the context: `mail.mail`
        # records outlive the composer transaction and are re-sent through
        # paths that know nothing about it (the standard Retry button ->
        # `mark_outgoing` + `process_email_queue`, the queue cron, a resend
        # wizard). Without the marker those sends fell back to core, which
        # rebuilds `To` from the `X-Msg-To-Add` header stored on the record --
        # a header that lists *every* external recipient, Bcc partners
        # included -- and merges it into `To` in
        # `IrMailServer._alter_message__`, disclosing the blind copies to
        # everybody and sending the Cc recipients a second, duplicate email.
        if not (
            self.is_composer_cc_bcc or self.env.context.get("is_from_composer", False)
        ):
            return res

        # In the absence of self.email_to, Odoo builds an extra Cc-only email
        # (see odoo/odoo@46bad8f0). Every Cc partner is also a recipient and
        # already gets its own email, so that one is a duplicate here.
        res = [m for m in res if m["email_to"]]

        # The To / Cc headers must be identical on every email, but no single
        # mail.mail knows the whole audience: followers never reach
        # partner_ids, and the notification may be split into one mail.mail
        # per lang. MailThread._notify_thread_by_email publishes the full
        # audience through the context (OCA/mail#233).
        partners_cc_bcc = self.recipient_cc_ids + self.recipient_bcc_ids
        # Fall back to this mail.mail's own recipients when the context is
        # absent (a direct _prepare_outgoing_list call that does not go
        # through _notify_thread_by_email): the audience is then unknown and
        # the pre-#233 behaviour is the only correct answer.
        all_recipients = (
            self.env["res.partner"].browse(
                self.env.context.get("composer_recipient_ids") or []
            )
            or self.recipient_ids
        )
        partner_to = all_recipients - partners_cc_bcc
        email_to = format_emails(partner_to)
        email_to_raw = format_emails_raw(partner_to)
        email_cc = format_emails_str(self.recipient_cc_ids)
        email_bcc = [r.email for r in self.recipient_bcc_ids if r.email]

        # Update all emails with the same To, Cc headers (to be shown by the
        # email client as users expect)
        for m in res:
            # Odoo reuses the headers dictionary for all outgoing entries:
            # copy it before adding recipient-specific headers. Odoo 19 also
            # adds every external recipient to 'X-Msg-To-Add', which
            # IrMailServer._alter_message__ merges into the To header to enable
            # Reply-All: here it would put the Cc *and the Bcc* recipients in To,
            # so drop it — this module builds the whole To / Cc itself.
            m["headers"] = {
                key: value
                for key, value in m["headers"].items()
                if key != "X-Msg-To-Add"
            }
            # A recipient partner with no email address yields the
            # '"Name" <@False>' placeholder that core builds on purpose
            # (MailMail._prepare_outgoing_list), from which no address can be
            # extracted. Leave that entry untouched: core then finds no SMTP
            # recipient for it, raises NO_VALID_RECIPIENT and skips only this
            # recipient, delivering the message to all the others. Indexing
            # blindly here used to raise IndexError and take the whole
            # mail.mail down with it.
            addresses = [
                address
                for address in extract_rfc2822_addresses(m["email_to"][0])
                if tools.mail.email_normalize(address)
            ]
            if not addresses:
                continue
            rcpt_to = addresses[0]

            # If the recipient is a Bcc, set a real Bcc header on its own
            # email only: IrMailServer._prepare_smtp_to_list uses it to build
            # the envelope and _alter_message__ strips it right after, so it
            # is never transmitted and cannot leak.
            if rcpt_to in email_bcc:
                m["headers"]["Bcc"] = m["email_to"][0]
                # Optional legacy marker. Unlike Bcc it survives sending, so
                # only add it when explicitly enabled (OCA/mail#233).
                if self._expose_bcc_marker():
                    m["headers"]["X-Odoo-Bcc"] = m["email_to"][0]

            m.update(
                {
                    "email_to": email_to,
                    "email_to_raw": email_to_raw,
                    "email_cc": email_cc,
                }
            )

        return res
