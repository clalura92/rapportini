const MONTHS = [
  'Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
  'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre',
]

export default function Header({ year, month }) {
  return (
    <header className="app-header">
      <div>
        <h1>Rapportini Generator</h1>
        <p>Solware — generazione rapportini e riassunti</p>
      </div>
      <span className="header-badge">
        {MONTHS[month - 1]} {year}
      </span>
    </header>
  )
}
