import { defineCloudflareConfig } from "@opennextjs/cloudflare";

// Minimal OpenNext Cloudflare configuration. No incremental cache binding: this
// app's API routes are force-dynamic ([D13]) and pages are server-rendered, so
// there is nothing worth caching across isolates yet.
export default defineCloudflareConfig({});
