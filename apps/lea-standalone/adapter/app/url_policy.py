"""Syntax validation for stored tool metadata. Worker resolves DNS at execution."""
import ipaddress
from urllib.parse import urlparse
class UrlRefused(ValueError):pass

def check_url(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname:raise UrlRefused('A public https URL is required.')
    host=parsed.hostname.lower()
    if host in {'localhost','metadata.google.internal'} or host.endswith(('.localhost','.local')):raise UrlRefused('Local endpoints are not permitted.')
    try:ip=ipaddress.ip_address(host)
    except ValueError:return url
    if not ip.is_global:raise UrlRefused('Private endpoints are not permitted.')
    return url
