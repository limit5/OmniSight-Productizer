"use client"

export function NeuralGrid() {
  return (
    <>
      {/* Base Neural Grid */}
      <div className="neural-grid" aria-hidden="true">
        <div className="absolute inset-0 bg-gradient-to-t from-[#010409] via-transparent to-transparent" />
      </div>
      
      {/* Hexagonal Pattern Overlay */}
      <div className="fixed inset-0 hex-pattern opacity-30 pointer-events-none z-0" aria-hidden="true" />
      
      {/* Data Stream Effect */}
      <div className="fixed inset-0 data-stream opacity-50 pointer-events-none z-0" aria-hidden="true" />
      
      {/* Subtle Digital Noise */}
      <div className="fixed inset-0 digital-noise pointer-events-none z-0" aria-hidden="true" />
      
      {/* Vignette Effect */}
      <div 
        className="fixed inset-0 pointer-events-none z-0" 
        aria-hidden="true"
        style={{
          background: 'radial-gradient(ellipse at center, transparent 0%, transparent 50%, rgba(1, 4, 9, 0.8) 100%)'
        }}
      />
    </>
  )
}
