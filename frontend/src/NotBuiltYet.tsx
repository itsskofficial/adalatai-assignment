export function NotBuiltYet({ name }: { name: string }) {
  return (
    <main className="screen">
      <h1>{name}</h1>
      <p className="empty">Not built yet.</p>
    </main>
  )
}
