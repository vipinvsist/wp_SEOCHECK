import requests
import streamlit as st
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup
import csv
import re
import sys
import time
import pandas as pd
import os
import pygsheets
from datetime import datetime, timedelta

def extract_domain_from_url(url):
    """
    Extract domain name from a given URL with improved error handling.
    Removes www. prefix to get the root domain.
    """
    try:
        if not url or not isinstance(url, str):
            return ""
        # Handle URLs without scheme
        if not url.startswith(('http://', 'https://')):
            if url.startswith('//'):
                url = 'https:' + url
            elif not url.startswith('/'):
                url = 'https://' + url
            else:
                return ""  # Relative URL
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain if domain else ""
    except Exception as e:
        print(f"Domain extraction error for {url}: {str(e)}")
        return ""

def fetch_webpage_content(url, timeout=30):
    """
    Fetch webpage content using requests with proper headers.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        return soup
    except requests.RequestException as e:
        raise requests.RequestException(f"Failed to fetch {url}: {str(e)}")
    except Exception as e:
        raise Exception(f"Error parsing content from {url}: {str(e)}")

def extract_rel_attributes(link_element):
    """
    Extract and analyze rel attributes from an anchor element.
    """
    rel_attr = link_element.get('rel', [])
    if isinstance(rel_attr, str):
        rel_attr = [rel_attr]
    rel_values = ' '.join(rel_attr).lower() if rel_attr else ''
    return {
        'noopener': 1 if 'noopener' in rel_values else 0,
        'noreferrer': 1 if 'noreferrer' in rel_values else 0,
        'nofollow': 1 if 'nofollow' in rel_values else 0,
        'dofollow': 1 if 'dofollow' in rel_values else 0
    }

def is_external_link(article_url, link_url):
    article_domain = extract_domain_from_url(article_url)
    link_domain = extract_domain_from_url(link_url)
    return link_domain

def analyze_article_interlinks(article_url):
    try:
        soup = fetch_webpage_content(article_url)
        start_element = soup.find(class_="main-heading mt-2 mb-4")
        stop_element = soup.find(class_="text-light-primary text-uppercase fs-18 pb-3 m-0")
        if not start_element or not stop_element:
            return []
        current_element = start_element
        elements_in_range = [start_element]
        while current_element and current_element != stop_element:
            current_element = current_element.find_next()
            if current_element:
                elements_in_range.append(current_element)
                if current_element == stop_element:
                    break
        links = []
        for element in elements_in_range:
            if element.name == 'a' and element.get('href'):
                links.append(element)
            nested_links = element.find_all('a', href=True) if hasattr(element, 'find_all') else []
            links.extend(nested_links)
        interlinks_data = []
        for link in links:
            href = link.get('href')
            if not href:
                continue
            if href.startswith('/'):
                href = urljoin(article_url, href)
            elif not href.startswith('http'):
                continue
            if not is_external_link(article_url, href):
                continue
            anchor_text = str(link.get_text()).strip()
            rel_attributes = extract_rel_attributes(link)
            interlink_data = {
                'Article_url': article_url,
                'Interlink_url': href,
                'Interlink_domain': extract_domain_from_url(href),
                'Anchor_text': anchor_text,
                **rel_attributes
            }
            interlinks_data.append(interlink_data)
        return interlinks_data
    except Exception as e:
        return []

def check_interlinks_strict(soup, article_url):
    issues = []
    problematic_texts = [
        'click here', 'here', 'read more', 'more', 'link', 'this', 'that',
        'continue reading', 'view more', 'see more', 'learn more', 'article', 'read this'
    ]
    article_domain = extract_domain_from_url(article_url)
    links = soup.find_all('a', href=True)
    for i, link in enumerate(links):
        href = link.get('href')
        if href.startswith('#h-'):
            continue
        anchor_text = link.get_text().strip().lower()
        rel_attrs = link.get('rel', [])
        if isinstance(rel_attrs, str):
            rel_attrs = rel_attrs.split()
        rel = set([r.lower() for r in rel_attrs])
        interlink_domain = extract_domain_from_url(href)
        # do_follow = int('dofollow' in rel)
        # print(20*"---",do_follow)
        no_follow = int('nofollow' in rel)
        noopener = int('noopener' in rel)
        noreferrer = int('noreferrer' in rel)
        element_details = f"Link {i + 1}: '{anchor_text}' -> {href}"
        if interlink_domain == "analyticsvidhya.com":
            if no_follow == 1:
                issues.append({
                    'Issue_Type': 'Interlink',
                    'Issue_Description': 'AV interlink must be dofollow',
                    'Element_Details': element_details
                })
            if anchor_text in problematic_texts:
                issues.append({
                    'Issue_Type': 'Interlink',
                    'Issue_Description': 'Non-descriptive anchor text for AV interlink',
                    'Element_Details': element_details
                })
        elif interlink_domain and interlink_domain != "":
            if no_follow != 1:
                issues.append({
                    'Issue_Type': 'Interlink',
                    'Issue_Description': 'External interlink missing nofollow',
                    'Element_Details': element_details
                })
        if noopener != 1:
            issues.append({
                'Issue_Type': 'Interlink',
                'Issue_Description': 'noopener must be set for all interlinks',
                'Element_Details': element_details
            })
        if interlink_domain and interlink_domain != article_domain:
            if noreferrer != 1:
                issues.append({
                    'Issue_Type': 'Interlink',
                    'Issue_Description': 'noreferrer must be set for links to other domains',
                    'Element_Details': element_details
                })
    return issues

class WordPressDraftQualityChecker:
    def __init__(self):
        self.results = []
        self.username = st.secrets['USERNAME']
        self.password = st.secrets['PASSWORD']

    def fetch_draft_content(self, wordpress_url):
        try:
            parsed = urlparse(wordpress_url)
            query_params = parse_qs(parsed.query)
            post_id_list = query_params.get("post", [None])
            if not post_id_list or not post_id_list[0]:
                raise ValueError("Could not extract post ID from URL")
            post_id = post_id_list[0]
            st.info(f"Fetching draft content for Post ID: {post_id}")
            draft_posts_response = requests.get(
                f"https://www.analyticsvidhya.com/wp-json/wp/v2/posts/{post_id}?status=draft",
                auth=(self.username, self.password)
            )
            if draft_posts_response.status_code != 200:
                st.warning(f"Draft endpoint failed with status {draft_posts_response.status_code}")
                alternative_response = requests.get(
                    f"https://www.analyticsvidhya.com/wp-json/wp/v2/posts/{post_id}",
                    auth=(self.username, self.password)
                )
                if alternative_response.status_code == 200:
                    st.info("Retrieved post using alternative endpoint (post might not be in draft status)")
                    draft_posts = alternative_response.json()
                else:
                    raise Exception(f"API request failed: {draft_posts_response.text}")
            else:
                draft_posts = draft_posts_response.json()
            article_content = draft_posts.get('content', {}).get('rendered', '')
            if not article_content:
                st.warning("No content found in the post.")
                return None, None, None
            return article_content, wordpress_url, draft_posts
        except Exception as e:
            st.error(f"Error fetching draft content: {str(e)}")
            return None, None, None

    def is_av_cdn_image(self, src):
        return (src.startswith('https://cdn.analyticsvidhya.com/wp-content/uploads') and
                src.lower().endswith(('.webp', '.gif')))

    def is_av_cdn_video(self, src):
        return (src.startswith('https://cdn.analyticsvidhya.com/wp-content/uploads') and
                src.lower().endswith('.mp4'))

    def is_av_cdn_media(self, src):
        return src.startswith('https://cdn.analyticsvidhya.com/wp-content/uploads')

    def is_internal_domain(self, url):
        if not url:
            return False
        internal_domains = ['analyticsvidhya.com']
        domain = extract_domain_from_url(url)
        return domain in internal_domains

    def check_image_format(self, soup, url):
        images = soup.find_all('img')
        webp_issues = []
        for i, img in enumerate(images):
            src = img.get('src', '')
            if src and self.is_av_cdn_media(src):
                if not self.is_av_cdn_image(src):
                    webp_issues.append({
                        'URL': url,
                        'Issue_Type': 'WebP Format',
                        'Issue_Description': 'Analytics Vidhya CDN image not in WebP format',
                        'Element_Details': f"Image {i + 1}: {src}"
                    })
        return webp_issues

    def check_alt_text(self, soup, url):
        images = soup.find_all('img')
        alt_issues = []
        for i, img in enumerate(images):
            src = img.get('src', '')
            alt = img.get('alt', '')
            if src and self.is_av_cdn_image(src):
                if not alt or alt.strip() == '':
                    alt_issues.append({
                        'URL': url,
                        'Issue_Type': 'Alt Text',
                        'Issue_Description': 'Missing alt text for Analytics Vidhya CDN image',
                        'Element_Details': f"Image {i + 1}: {src}"
                    })
        return alt_issues

    def check_meta_description(self, draft_data, url):
        meta_issues = []
        try:
            yoast_head = draft_data.get('yoast_head_json', {})
            og_description = yoast_head.get('og_description', '')
            if not og_description or not og_description.strip():
                meta_issues.append({
                    'URL': url,
                    'Issue_Type': 'Meta Description',
                    'Issue_Description': 'Missing og:description meta tag',
                    'Element_Details': 'N/A'
                })
        except Exception as e:
            meta_issues.append({
                'URL': url,
                'Issue_Type': 'Meta Description',
                'Issue_Description': f'Error checking meta description: {str(e)}',
                'Element_Details': 'N/A'
            })
        return meta_issues

    def check_lazy_loading(self, soup, url):
        lazy_issues = []
        images = soup.find_all('img')
        for i, img in enumerate(images):
            src = img.get('src', '')
            loading = img.get('loading', '')
            if src and self.is_av_cdn_image(src):
                if loading != 'lazy':
                    lazy_issues.append({
                        'URL': url,
                        'Issue_Type': 'Lazy Loading',
                        'Issue_Description': 'Missing lazy loading for Analytics Vidhya CDN image',
                        'Element_Details': f"Image {i + 1}: {src}"
                    })
        videos = soup.find_all('video')
        for i, video in enumerate(videos):
            src = video.get('src', '')
            loading = video.get('loading', '')
            if src and self.is_av_cdn_video(src):
                if loading != 'lazy':
                    lazy_issues.append({
                        'URL': url,
                        'Issue_Type': 'Lazy Loading',
                        'Issue_Description': 'Missing lazy loading for Analytics Vidhya CDN video',
                        'Element_Details': f"Video {i + 1}: {src}"
                    })
            sources = video.find_all('source')
            for j, source in enumerate(sources):
                source_src = source.get('src', '')
                if source_src and self.is_av_cdn_video(source_src):
                    if loading != 'lazy':
                        lazy_issues.append({
                            'URL': url,
                            'Issue_Type': 'Lazy Loading',
                            'Issue_Description': 'Missing lazy loading for Analytics Vidhya CDN video',
                            'Element_Details': f"Video {i + 1}, Source {j + 1}: {source_src}"
                        })
        iframes = soup.find_all('iframe')
        for i, iframe in enumerate(iframes):
            src = iframe.get('src', '')
            loading = iframe.get('loading', '')
            if src and self.is_av_cdn_video(src):
                if loading != 'lazy':
                    lazy_issues.append({
                        'URL': url,
                        'Issue_Type': 'Lazy Loading',
                        'Issue_Description': 'Missing lazy loading for Analytics Vidhya CDN video iframe',
                        'Element_Details': f"Iframe {i + 1}: {src}"
                    })
        return lazy_issues

    def analyze_content(self, article_content, url, draft_data=None):
        try:
            soup = BeautifulSoup(article_content, 'html.parser')
            all_issues = []
            webp_issues = self.check_image_format(soup, url)
            alt_issues = self.check_alt_text(soup, url)
            meta_issues = self.check_meta_description(draft_data, url)
            lazy_issues = self.check_lazy_loading(soup, url)
            interlink_issues = check_interlinks_strict(soup, url)
            all_issues.extend(webp_issues)
            all_issues.extend(alt_issues)
            all_issues.extend(meta_issues)
            all_issues.extend(lazy_issues)
            all_issues.extend(interlink_issues)
            return all_issues
        except Exception as e:
            st.error(f"Error analyzing content: {str(e)}")
            return []

def main():
    st.set_page_config(page_title="WordPress Draft Quality Checker",
                      page_icon="🔍", layout="wide")
    st.title("WordPress Draft Content Quality Checker")
    st.write("Enter a WordPress draft URL to analyze Analytics Vidhya CDN content quality issues and link attributes.")
    wordpress_url = st.text_input(
        "Enter the WordPress URL:",
        placeholder="https://www.analyticsvidhya.com/wp-admin/post.php?post=12345&action=edit",
        help="Enter the WordPress admin edit URL for the draft post"
    )
    if st.button("Analyze Content", type="primary"):
        if not wordpress_url:
            st.warning("Please enter a WordPress URL.")
            return
        try:
            checker = WordPressDraftQualityChecker()
        except Exception as e:
            st.error(f"Error loading credentials: {str(e)}")
            st.error("Make sure USERNAME and PASSWORD are set in .streamlit/secrets.toml")
            return
        with st.spinner("Fetching draft content..."):
            article_content, url, draft_data = checker.fetch_draft_content(wordpress_url)
        if article_content and draft_data:
            with st.spinner("Analyzing content quality..."):
                issues = checker.analyze_content(article_content, url, draft_data)
            if issues:
                df_results = pd.DataFrame(issues)
                filtered_df = df_results
                # Separate interlinking issues from other issues
                interlink_issues = filtered_df[filtered_df['Issue_Type'] == 'Interlink']
                other_issues = filtered_df[filtered_df['Issue_Type'] != 'Interlink']
                # Display interlinking issues table
                if not interlink_issues.empty:
                    st.subheader("🔗 Interlinking Issues")
                    st.dataframe(interlink_issues, use_container_width=True, hide_index=True)
                    st.write(f"**Total Interlinking Issues:** {len(interlink_issues)}")
                # Display other quality issues table
                if not other_issues.empty:
                    st.subheader("📷 SEO Quality Issues")
                    st.dataframe(other_issues, use_container_width=True, hide_index=True)
                    st.write(f"**Total SEO Issues:** {len(other_issues)}")
                # If no issues found in categories, show all
                if interlink_issues.empty and other_issues.empty and not filtered_df.empty:
                    st.subheader("All Issues")
                    st.dataframe(filtered_df, use_container_width=True, hide_index=True)
                # Provide download option
                csv = df_results.to_csv(index=False)
                st.download_button(
                    label="Download Full Results as CSV",
                    data=csv,
                    file_name=f"wordpress_quality_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime='text/csv'
                )
            else:
                st.success("🎉 No quality issues found! The Analytics Vidhya content looks excellent.")
                st.balloons()
        else:
            st.error("Failed to fetch or analyze the content. Please check the URL and credentials, then try again.")

if __name__ == "__main__":
    main()
