import argparse

import psycopg

from app.settings import get_settings

SAMPLE_REQUESTS = [
    {
        "requester_name": "John Smith",
        "requester_email": "john.smith@derisk360.local",
        "department": "Software Engineering",
        "requested_tool": "Figma Professional",
        "vendor_name": "Figma",
        "category": "Design",
        "estimated_amount": 1800,
        "currency_code": "AED",
        "business_justification": "Design collaboration and prototyping for the customer portal redesign project.",
        "email_subject": "Request for Figma License",
        "message_body": (
            "Hello,\n\n"
            "I would like access to Figma Professional for UI/UX design work on the customer portal redesign project.\n\n"
            "Department:\nSoftware Engineering\n\n"
            "Business Justification:\nDesign collaboration and prototyping.\n\n"
            "Thank you,\nJohn Smith"
        ),
        "status": "new",
    },
    {
        "requester_name": "Sarah Ahmed",
        "requester_email": "sarah.ahmed@derisk360.local",
        "department": "Marketing",
        "requested_tool": "Adobe Creative Cloud",
        "vendor_name": "Adobe",
        "category": "Design",
        "estimated_amount": 4200,
        "currency_code": "AED",
        "business_justification": "Campaign asset creation and brand design for Q3 product launch.",
        "email_subject": "Adobe Creative Cloud Team License Request",
        "message_body": (
            "Hi IT Team,\n\n"
            "Please provision Adobe Creative Cloud for the marketing design team.\n\n"
            "Department:\nMarketing\n\n"
            "Business Justification:\nCampaign asset creation and brand design.\n\n"
            "Thanks,\nSarah Ahmed"
        ),
        "status": "under_review",
    },
    {
        "requester_name": "Michael Chen",
        "requester_email": "michael.chen@derisk360.local",
        "department": "Product",
        "requested_tool": "Miro Enterprise",
        "vendor_name": "Miro",
        "category": "Productivity",
        "estimated_amount": 2400,
        "currency_code": "AED",
        "business_justification": "Cross-functional roadmap workshops and product discovery sessions.",
        "email_subject": "Request for Miro Enterprise",
        "message_body": (
            "Hello IT Admin,\n\n"
            "We need Miro Enterprise for product discovery and roadmap planning workshops.\n\n"
            "Department:\nProduct\n\n"
            "Business Justification:\nCross-functional collaboration and discovery.\n\n"
            "Regards,\nMichael Chen"
        ),
        "status": "new",
    },
    {
        "requester_name": "Emily Watson",
        "requester_email": "emily.watson@derisk360.local",
        "department": "Human Resources",
        "requested_tool": "Notion Team",
        "vendor_name": "Notion",
        "category": "Productivity",
        "estimated_amount": 960,
        "currency_code": "AED",
        "business_justification": "Centralised onboarding playbooks, policy docs, and HR knowledge base for new hires.",
        "email_subject": "Notion Team License for HR",
        "message_body": (
            "Hi,\n\n"
            "Please approve Notion Team for the HR department to manage onboarding and policy documentation.\n\n"
            "Department:\nHuman Resources\n\n"
            "Business Justification:\nOnboarding playbooks and internal knowledge base.\n\n"
            "Thanks,\nEmily Watson"
        ),
        "status": "new",
    },
    {
        "requester_name": "David Okonkwo",
        "requester_email": "david.okonkwo@derisk360.local",
        "department": "Operations",
        "requested_tool": "Zoom Workplace",
        "vendor_name": "Zoom",
        "category": "Communication",
        "estimated_amount": 1500,
        "currency_code": "AED",
        "business_justification": "Client workshops and vendor coordination across regional offices.",
        "email_subject": "Zoom Workplace for Operations Team",
        "message_body": (
            "Hello IT,\n\n"
            "We need Zoom Workplace for recurring client workshops and cross-office coordination.\n\n"
            "Department:\nOperations\n\n"
            "Business Justification:\nClient workshops and vendor meetings.\n\n"
            "Best,\nDavid Okonkwo"
        ),
        "status": "new",
    },
    {
        "requester_name": "Priya Sharma",
        "requester_email": "priya.sharma@derisk360.local",
        "department": "Software Engineering",
        "requested_tool": "GitHub Enterprise",
        "vendor_name": "GitHub",
        "category": "Development",
        "estimated_amount": 6800,
        "currency_code": "AED",
        "business_justification": "Secure code hosting, CI integration, and compliance controls for engineering teams.",
        "email_subject": "GitHub Enterprise Subscription Request",
        "message_body": (
            "Hi IT Admin,\n\n"
            "Engineering requires GitHub Enterprise for secure repos, SSO, and audit logging.\n\n"
            "Department:\nSoftware Engineering\n\n"
            "Business Justification:\nSecure development platform with compliance controls.\n\n"
            "Regards,\nPriya Sharma"
        ),
        "status": "under_review",
    },
    {
        "requester_name": "James Wilson",
        "requester_email": "james.wilson@derisk360.local",
        "department": "Marketing",
        "requested_tool": "Canva Pro",
        "vendor_name": "Canva",
        "category": "Design",
        "estimated_amount": 540,
        "currency_code": "AED",
        "business_justification": "Social media graphics and quick campaign assets for the demand gen team.",
        "email_subject": "Canva Pro for Marketing",
        "message_body": (
            "Hello,\n\n"
            "Please provision Canva Pro for social and demand gen content creation.\n\n"
            "Department:\nMarketing\n\n"
            "Business Justification:\nSocial media and campaign asset production.\n\n"
            "Thanks,\nJames Wilson"
        ),
        "status": "new",
    },
    {
        "requester_name": "Lisa Nguyen",
        "requester_email": "lisa.nguyen@derisk360.local",
        "department": "Product",
        "requested_tool": "Jira Software Premium",
        "vendor_name": "Atlassian",
        "category": "Project Management",
        "estimated_amount": 3200,
        "currency_code": "AED",
        "business_justification": "Sprint planning, backlog management, and delivery tracking for product squads.",
        "email_subject": "Jira Software Premium for Product Squads",
        "message_body": (
            "Hi IT Team,\n\n"
            "Product squads need Jira Software Premium for sprint planning and delivery tracking.\n\n"
            "Department:\nProduct\n\n"
            "Business Justification:\nBacklog management and agile delivery.\n\n"
            "Lisa Nguyen"
        ),
        "status": "new",
    },
]


def seed_tool_requests(*, reset: bool = False) -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            if reset:
                cur.execute("DELETE FROM slmct.tool_requests")
                print("Cleared existing tool requests.")
            else:
                cur.execute("SELECT COUNT(*) FROM slmct.tool_requests")
                existing = cur.fetchone()[0]
                if existing:
                    print(f"Tool requests already seeded ({existing} rows). Skipping.")
                    print("Run with --reset to replace demo tool requests.")
                    return

            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for tool request seeding.")
                return

            org_id = org[0]
            for request in SAMPLE_REQUESTS:
                cur.execute(
                    """
                    INSERT INTO slmct.tool_requests (
                      organisation_id, requester_name, requester_email, department,
                      requested_tool, vendor_name, category, estimated_amount, currency_code,
                      business_justification, message_body, email_subject, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        org_id,
                        request["requester_name"],
                        request["requester_email"],
                        request["department"],
                        request["requested_tool"],
                        request["vendor_name"],
                        request["category"],
                        request["estimated_amount"],
                        request["currency_code"],
                        request["business_justification"],
                        request["message_body"],
                        request["email_subject"],
                        request["status"],
                    ),
                )
        conn.commit()
    print(f"Seeded {len(SAMPLE_REQUESTS)} demo tool requests.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo tool requests for the Tool Requests inbox.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing tool requests and re-seed the full demo set.",
    )
    args = parser.parse_args()
    seed_tool_requests(reset=args.reset)


if __name__ == "__main__":
    main()
