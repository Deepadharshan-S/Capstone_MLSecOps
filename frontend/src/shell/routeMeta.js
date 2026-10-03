// Route → topbar title/breadcrumb. Kept out of Sidebar.jsx so that file
// exports components only (fast-refresh friendly).
export function routeMeta(pathname) {
  const seg = pathname.split('/').filter(Boolean)
  const base = seg[0] || ''
  const TITLES = {
    '': 'Dashboard',
    pipeline: 'Pipeline',
    datasets: 'Datasets',
    jobs: 'Training Jobs',
    registry: 'Model Registry',
    deployments: 'Deployments',
    predict: 'Predict',
    experiments: 'Experiments',
    admin: 'Admin',
  }
  const title = TITLES[base] || 'SentinelML'
  const crumbs = [{ label: 'SentinelML', to: '/' }, { label: title }]
  if (base === 'datasets' && seg[1]) crumbs.push({ label: decodeURIComponent(seg[1]) })
  if (base === 'jobs' && seg[1]) crumbs.push({ label: seg[1] })
  if (base === 'registry' && seg[1]) crumbs.push({ label: decodeURIComponent(seg[1]) })
  return { title, crumbs }
}
