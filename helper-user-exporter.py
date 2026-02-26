import oci
import json

def get_identity_domains(config, identity_client):
    domains = []
    try:
        response = identity_client.list_domains(compartment_id=config["tenancy"])
        domains = response.data
    except Exception as e:
        print(f"Error fetching domains: {e}")
    return domains

def get_domain_users(domain_url, config):
    users = []
    identity_domains_client = oci.identity_domains.IdentityDomainsClient(
        config, service_endpoint=domain_url
    )
    
    start_index = 1
    count = 100
    
    while True:
        try:
            response = identity_domains_client.list_users(start_index=start_index, count=count)
            resources = response.data.resources or []
            users.extend(resources)
            
            total = response.data.total_results or 0
            print(f"    Fetched {len(users)}/{total} users...")
            
            if start_index + count - 1 >= total:
                break
            start_index += count
        except Exception as e:
            print(f"  Error fetching users: {e}")
            break
    
    return users

def format_user(raw_user):
    username = raw_user.user_name or ""
    
    first_name = ""
    last_name = ""
    if raw_user.name:
        first_name = raw_user.name.given_name or ""
        last_name = raw_user.name.family_name or ""
    
    email = ""
    if raw_user.emails:
        for e in raw_user.emails:
            if e.primary:
                email = e.value or ""
                break
        if not email:
            email = raw_user.emails[0].value or ""
    
    active = raw_user.active if raw_user.active is not None else True
    state = "present" if active else "absent"
    
    return {
        "username": username,
        "state": state,
        "profile": "",
        "groups": [],
        "first_name": first_name,
        "last_name": last_name,
        "email": email
    }

def write_json(result, output_file):
    with open(output_file, "w", encoding="utf-8") as f:
        f.write('{\n    "domains": {\n')
        domains_list = list(result["domains"].items())
        for d_idx, (d_key, d_val) in enumerate(domains_list):
            d_comma = "," if d_idx < len(domains_list) - 1 else ""
            f.write(f'        {json.dumps(d_key)}: {{\n')
            f.write(f'            "domain_name": {json.dumps(d_val["domain_name"])},\n')
            f.write(f'            "domain_url": {json.dumps(d_val["domain_url"])},\n')
            f.write(f'            "users": [\n')
            users = d_val["users"]
            for u_idx, user in enumerate(users):
                u_comma = "," if u_idx < len(users) - 1 else ""
                # Serialize user as a compact single line
                user_line = json.dumps(user, ensure_ascii=False, separators=(", ", ": "))
                # Fix separators to have space after colon but not comma
                user_line = json.dumps(user, ensure_ascii=False)
                # Force compact (no newlines) by using default json.dumps without indent
                f.write(f'                {user_line}{u_comma}\n')
            f.write('            ]\n')
            f.write(f'        }}{d_comma}\n')
        f.write('    }\n}\n')

def export_tenant_users():
    config = oci.config.from_file()
    identity_client = oci.identity.IdentityClient(config)
    
    print("Fetching identity domains...")
    domains = get_identity_domains(config, identity_client)
    
    if not domains:
        print("No domains found.")
        return
    
    result = {"domains": {}}
    
    for domain in domains:
        domain_name = domain.display_name
        domain_url = domain.url.rstrip("/")
        
        print(f"\nProcessing domain: {domain_name} ({domain_url})")
        
        raw_users = get_domain_users(domain_url, config)
        print(f"  Total: {len(raw_users)} users")
        
        formatted_users = []
        for raw_user in raw_users:
            try:
                formatted_users.append(format_user(raw_user))
            except Exception as e:
                print(f"  Error processing user {raw_user.user_name}: {e}")
        
        result["domains"][domain_name] = {
            "domain_name": domain_name,
            "domain_url": domain_url,
            "users": formatted_users
        }
    
    output_file = "tenant_users_export.json"
    write_json(result, output_file)
    print(f"\nExport complete! Saved to {output_file}")
    return result

if __name__ == "__main__":
    export_tenant_users()
