(function () {
  'use strict';

  const languages = {
    en: { label: 'English', dir: 'ltr' },
    ar: { label: 'العربية', dir: 'rtl' },
    tr: { label: 'Türkçe', dir: 'ltr' },
    ru: { label: 'Русский', dir: 'ltr' },
    es: { label: 'Español', dir: 'ltr' },
  };

  const navEnglish = {
    dashboard:'Dashboard', project_dashboard:'Project Dashboard', projects:'Projects', tasks:'Tasks', meetings:'Meeting Requests',
    leads:'Leads', opportunities:'Opportunities', customers:'Customers', quotations:'Quotations', sales_orders:'Sales Orders',
    sales_invoices:'Sales Invoices', delivery_notes:'Delivery Notes', payments:'Payments', suppliers:'Suppliers',
    purchase_orders:'Purchase Orders', purchase_invoices:'Purchase Invoices', stock_dashboard:'Stock Dashboard', items:'Items',
    stock_reports:'Stock Reports', assets:'Asset Register', depreciation:'Depreciation', bank_accounts:'Bank Accounts',
    transactions:'Transactions', import_statement:'Import Statement', reconciliation:'Reconciliation', journals:'Journal Entries',
    chart:'Chart of Accounts', reports:'Report Hub', hr_dashboard:'HR Dashboard', employees:'Employees', checkin:'Check In / Out',
    attendance:'Attendance', leave:'Leave', loans:'Loan Applications', salary:'Salary Slips', salary_components:'Salary Components',
    salary_structure:'Salary Structure', structure_assign:'Structure Assign', payroll:'Run Payroll', expenses:'Expense Claims',
    jobs:'Job Openings', company:'Company', profile:'My Profile', settings:'Company Settings',
  };

  const nav = {
    ar: {
      dashboard:'لوحة التحكم',project_dashboard:'لوحة المشاريع',projects:'المشاريع',tasks:'المهام',meetings:'طلبات الاجتماعات',leads:'العملاء المحتملون',opportunities:'الفرص',customers:'العملاء',quotations:'عروض الأسعار',sales_orders:'أوامر البيع',sales_invoices:'فواتير المبيعات',delivery_notes:'إشعارات التسليم',payments:'المدفوعات',suppliers:'الموردون',purchase_orders:'أوامر الشراء',purchase_invoices:'فواتير المشتريات',stock_dashboard:'لوحة المخزون',items:'الأصناف',stock_reports:'تقارير المخزون',assets:'سجل الأصول',depreciation:'الإهلاك',bank_accounts:'الحسابات البنكية',transactions:'المعاملات',import_statement:'استيراد كشف الحساب',reconciliation:'التسوية',journals:'قيود اليومية',chart:'دليل الحسابات',reports:'مركز التقارير',hr_dashboard:'لوحة الموارد البشرية',employees:'الموظفون',checkin:'الحضور والانصراف',attendance:'الحضور',leave:'الإجازات',loans:'طلبات القروض',salary:'قسائم الرواتب',salary_components:'مكونات الراتب',salary_structure:'هيكل الراتب',structure_assign:'تعيين الهيكل',payroll:'تشغيل الرواتب',expenses:'مطالبات المصروفات',jobs:'الوظائف الشاغرة',company:'الشركة',profile:'ملفي الشخصي',settings:'إعدادات الشركة'
    },
    tr: {
      dashboard:'Kontrol Paneli',project_dashboard:'Proje Paneli',projects:'Projeler',tasks:'Görevler',meetings:'Toplantı Talepleri',leads:'Potansiyel Müşteriler',opportunities:'Fırsatlar',customers:'Müşteriler',quotations:'Teklifler',sales_orders:'Satış Siparişleri',sales_invoices:'Satış Faturaları',delivery_notes:'Teslimat Notları',payments:'Ödemeler',suppliers:'Tedarikçiler',purchase_orders:'Satın Alma Siparişleri',purchase_invoices:'Alış Faturaları',stock_dashboard:'Stok Paneli',items:'Ürünler',stock_reports:'Stok Raporları',assets:'Varlık Kaydı',depreciation:'Amortisman',bank_accounts:'Banka Hesapları',transactions:'İşlemler',import_statement:'Ekstre İçe Aktar',reconciliation:'Mutabakat',journals:'Yevmiye Kayıtları',chart:'Hesap Planı',reports:'Rapor Merkezi',hr_dashboard:'İK Paneli',employees:'Çalışanlar',checkin:'Giriş / Çıkış',attendance:'Devam',leave:'İzin',loans:'Kredi Başvuruları',salary:'Maaş Bordroları',salary_components:'Maaş Bileşenleri',salary_structure:'Maaş Yapısı',structure_assign:'Yapı Atama',payroll:'Bordro Çalıştır',expenses:'Masraf Talepleri',jobs:'Açık Pozisyonlar',company:'Şirket',profile:'Profilim',settings:'Şirket Ayarları'
    },
    ru: {
      dashboard:'Панель управления',project_dashboard:'Панель проектов',projects:'Проекты',tasks:'Задачи',meetings:'Запросы на встречи',leads:'Лиды',opportunities:'Сделки',customers:'Клиенты',quotations:'Коммерческие предложения',sales_orders:'Заказы на продажу',sales_invoices:'Счета продаж',delivery_notes:'Накладные',payments:'Платежи',suppliers:'Поставщики',purchase_orders:'Заказы на закупку',purchase_invoices:'Счета закупок',stock_dashboard:'Панель склада',items:'Товары',stock_reports:'Складские отчёты',assets:'Реестр активов',depreciation:'Амортизация',bank_accounts:'Банковские счета',transactions:'Операции',import_statement:'Импорт выписки',reconciliation:'Сверка',journals:'Журнальные записи',chart:'План счетов',reports:'Центр отчётов',hr_dashboard:'Панель кадров',employees:'Сотрудники',checkin:'Вход / Выход',attendance:'Посещаемость',leave:'Отпуска',loans:'Заявки на займы',salary:'Расчётные листки',salary_components:'Компоненты зарплаты',salary_structure:'Структура зарплаты',structure_assign:'Назначение структуры',payroll:'Расчёт зарплаты',expenses:'Заявки на расходы',jobs:'Вакансии',company:'Компания',profile:'Мой профиль',settings:'Настройки компании'
    },
    es: {
      dashboard:'Panel de control',project_dashboard:'Panel de proyectos',projects:'Proyectos',tasks:'Tareas',meetings:'Solicitudes de reunión',leads:'Clientes potenciales',opportunities:'Oportunidades',customers:'Clientes',quotations:'Cotizaciones',sales_orders:'Pedidos de venta',sales_invoices:'Facturas de venta',delivery_notes:'Notas de entrega',payments:'Pagos',suppliers:'Proveedores',purchase_orders:'Órdenes de compra',purchase_invoices:'Facturas de compra',stock_dashboard:'Panel de inventario',items:'Artículos',stock_reports:'Informes de inventario',assets:'Registro de activos',depreciation:'Depreciación',bank_accounts:'Cuentas bancarias',transactions:'Transacciones',import_statement:'Importar extracto',reconciliation:'Conciliación',journals:'Asientos contables',chart:'Plan de cuentas',reports:'Centro de informes',hr_dashboard:'Panel de RR. HH.',employees:'Empleados',checkin:'Entrada / Salida',attendance:'Asistencia',leave:'Permisos',loans:'Solicitudes de préstamo',salary:'Recibos de salario',salary_components:'Componentes salariales',salary_structure:'Estructura salarial',structure_assign:'Asignar estructura',payroll:'Ejecutar nómina',expenses:'Reclamaciones de gastos',jobs:'Vacantes',company:'Empresa',profile:'Mi perfil',settings:'Configuración de empresa'
    },
  };

  const common = {
    ar: {'Overview':'نظرة عامة','Projects & Tasks':'المشاريع والمهام','Team':'الفريق','Sales & Customers':'المبيعات والعملاء','Buying & Procurement':'المشتريات والتوريد','Stock & Inventory':'المخزون','Fixed Assets':'الأصول الثابتة','Banking':'الخدمات المصرفية','Accountant':'المحاسبة','Reports':'التقارير','HR Module':'الموارد البشرية','Settings':'الإعدادات','Finance':'المالية','Selling':'المبيعات','Buying':'المشتريات','Stock':'المخزون','Assets':'الأصول','Refresh':'تحديث','New':'جديد','Save':'حفظ','Update':'تحديث','Cancel':'إلغاء','Close':'إغلاق','Edit':'تعديل','Create':'إنشاء','Search':'بحث','Loading…':'جارٍ التحميل…','No records':'لا توجد سجلات','English':'الإنجليزية','Language':'اللغة','Notifications':'الإشعارات','Good morning':'صباح الخير','Good afternoon':'مساء الخير','Good evening':'مساء الخير'},
    tr: {'Overview':'Genel Bakış','Projects & Tasks':'Projeler ve Görevler','Team':'Ekip','Sales & Customers':'Satış ve Müşteriler','Buying & Procurement':'Satın Alma ve Tedarik','Stock & Inventory':'Stok ve Envanter','Fixed Assets':'Sabit Varlıklar','Banking':'Bankacılık','Accountant':'Muhasebe','Reports':'Raporlar','HR Module':'İnsan Kaynakları','Settings':'Ayarlar','Finance':'Finans','Selling':'Satış','Buying':'Satın Alma','Stock':'Stok','Assets':'Varlıklar','Refresh':'Yenile','New':'Yeni','Save':'Kaydet','Update':'Güncelle','Cancel':'İptal','Close':'Kapat','Edit':'Düzenle','Create':'Oluştur','Search':'Ara','Loading…':'Yükleniyor…','No records':'Kayıt yok','English':'İngilizce','Language':'Dil','Notifications':'Bildirimler','Good morning':'Günaydın','Good afternoon':'İyi günler','Good evening':'İyi akşamlar'},
    ru: {'Overview':'Обзор','Projects & Tasks':'Проекты и задачи','Team':'Команда','Sales & Customers':'Продажи и клиенты','Buying & Procurement':'Закупки и снабжение','Stock & Inventory':'Склад и запасы','Fixed Assets':'Основные средства','Banking':'Банкинг','Accountant':'Бухгалтерия','Reports':'Отчёты','HR Module':'Кадры','Settings':'Настройки','Finance':'Финансы','Selling':'Продажи','Buying':'Закупки','Stock':'Склад','Assets':'Активы','Refresh':'Обновить','New':'Создать','Save':'Сохранить','Update':'Обновить','Cancel':'Отмена','Close':'Закрыть','Edit':'Изменить','Create':'Создать','Search':'Поиск','Loading…':'Загрузка…','No records':'Нет записей','English':'Английский','Language':'Язык','Notifications':'Уведомления','Good morning':'Доброе утро','Good afternoon':'Добрый день','Good evening':'Добрый вечер'},
    es: {'Overview':'Resumen','Projects & Tasks':'Proyectos y tareas','Team':'Equipo','Sales & Customers':'Ventas y clientes','Buying & Procurement':'Compras y abastecimiento','Stock & Inventory':'Existencias e inventario','Fixed Assets':'Activos fijos','Banking':'Banca','Accountant':'Contabilidad','Reports':'Informes','HR Module':'Recursos humanos','Settings':'Configuración','Finance':'Finanzas','Selling':'Ventas','Buying':'Compras','Stock':'Inventario','Assets':'Activos','Refresh':'Actualizar','New':'Nuevo','Save':'Guardar','Update':'Actualizar','Cancel':'Cancelar','Close':'Cerrar','Edit':'Editar','Create':'Crear','Search':'Buscar','Loading…':'Cargando…','No records':'Sin registros','English':'Inglés','Language':'Idioma','Notifications':'Notificaciones','Good morning':'Buenos días','Good afternoon':'Buenas tardes','Good evening':'Buenas noches'},
  };

  const ui = {
    ar: {
      'Name':'الاسم','Type':'النوع','VAT / TRN':'ضريبة القيمة المضافة / الرقم الضريبي','Mobile':'الهاتف المحمول','Email':'البريد الإلكتروني','Connections':'الروابط','View':'عرض','Individual':'فرد','Customer':'العميل','Supplier':'المورد','Date':'التاريخ','Status':'الحالة','Amount':'المبلغ','Total':'الإجمالي','Currency':'العملة','Action':'الإجراء','Description':'الوصف','Account':'الحساب','Balance':'الرصيد','Debit':'مدين','Credit':'دائن','Reference':'المرجع','Posting Date':'تاريخ القيد','Due Date':'تاريخ الاستحقاق','Customer Name':'اسم العميل','Item':'الصنف','Quantity':'الكمية','Qty':'الكمية','Rate':'السعر','Tax':'الضريبة','Grand Total':'الإجمالي الكلي','Paid':'مدفوع','Unpaid':'غير مدفوع','Overdue':'متأخر','Draft':'مسودة','Submitted':'معتمد','Cancelled':'ملغى','Completed':'مكتمل','Active':'نشط','Inactive':'غير نشط','Open':'مفتوح','Closed':'مغلق','Pending':'قيد الانتظار','Approved':'موافق عليه','Rejected':'مرفوض','Yes':'نعم','No':'لا','From':'من','To':'إلى','Country':'الدولة','City':'المدينة','Address':'العنوان','Phone':'الهاتف','Group':'المجموعة','Territory':'المنطقة','Category':'الفئة','Department':'القسم','Employee':'الموظف','Project':'المشروع','Task':'المهمة','Priority':'الأولوية','Progress':'التقدم','Start Date':'تاريخ البدء','End Date':'تاريخ الانتهاء','Details':'التفاصيل','Filter':'تصفية','Clear filter':'مسح التصفية','Add':'إضافة','Delete':'حذف','Print':'طباعة','Download':'تنزيل','Submit':'إرسال','Back':'رجوع','Next':'التالي','New Customer':'عميل جديد','Save Customer':'حفظ العميل','Manage your customer records':'إدارة سجلات العملاء','Search customers, invoices, items, tasks…':'ابحث عن العملاء والفواتير والأصناف والمهام…','Open navigation':'فتح التنقل','Close navigation':'إغلاق التنقل'
    },
    tr: {
      'Name':'Ad','Type':'Tür','VAT / TRN':'KDV / Vergi No','Mobile':'Cep Telefonu','Email':'E-posta','Connections':'Bağlantılar','View':'Görüntüle','Individual':'Bireysel','Customer':'Müşteri','Supplier':'Tedarikçi','Date':'Tarih','Status':'Durum','Amount':'Tutar','Total':'Toplam','Currency':'Para Birimi','Action':'İşlem','Description':'Açıklama','Account':'Hesap','Balance':'Bakiye','Debit':'Borç','Credit':'Alacak','Reference':'Referans','Posting Date':'Kayıt Tarihi','Due Date':'Vade Tarihi','Customer Name':'Müşteri Adı','Item':'Ürün','Quantity':'Miktar','Qty':'Adet','Rate':'Birim Fiyat','Tax':'Vergi','Grand Total':'Genel Toplam','Paid':'Ödendi','Unpaid':'Ödenmedi','Overdue':'Gecikmiş','Draft':'Taslak','Submitted':'Onaylandı','Cancelled':'İptal Edildi','Completed':'Tamamlandı','Active':'Aktif','Inactive':'Pasif','Open':'Açık','Closed':'Kapalı','Pending':'Bekliyor','Approved':'Onaylandı','Rejected':'Reddedildi','Yes':'Evet','No':'Hayır','From':'Başlangıç','To':'Bitiş','Country':'Ülke','City':'Şehir','Address':'Adres','Phone':'Telefon','Group':'Grup','Territory':'Bölge','Category':'Kategori','Department':'Departman','Employee':'Çalışan','Project':'Proje','Task':'Görev','Priority':'Öncelik','Progress':'İlerleme','Start Date':'Başlangıç Tarihi','End Date':'Bitiş Tarihi','Details':'Ayrıntılar','Filter':'Filtre','Clear filter':'Filtreyi Temizle','Add':'Ekle','Delete':'Sil','Print':'Yazdır','Download':'İndir','Submit':'Gönder','Back':'Geri','Next':'İleri','New Customer':'Yeni Müşteri','Save Customer':'Müşteriyi Kaydet','Manage your customer records':'Müşteri kayıtlarınızı yönetin','Search customers, invoices, items, tasks…':'Müşteri, fatura, ürün ve görev ara…','Open navigation':'Menüyü aç','Close navigation':'Menüyü kapat'
    },
    ru: {
      'Name':'Название','Type':'Тип','VAT / TRN':'НДС / налоговый номер','Mobile':'Мобильный','Email':'Эл. почта','Connections':'Связи','View':'Открыть','Individual':'Физическое лицо','Customer':'Клиент','Supplier':'Поставщик','Date':'Дата','Status':'Статус','Amount':'Сумма','Total':'Итого','Currency':'Валюта','Action':'Действие','Description':'Описание','Account':'Счёт','Balance':'Баланс','Debit':'Дебет','Credit':'Кредит','Reference':'Ссылка','Posting Date':'Дата проводки','Due Date':'Срок оплаты','Customer Name':'Имя клиента','Item':'Товар','Quantity':'Количество','Qty':'Кол-во','Rate':'Ставка','Tax':'Налог','Grand Total':'Общий итог','Paid':'Оплачено','Unpaid':'Не оплачено','Overdue':'Просрочено','Draft':'Черновик','Submitted':'Проведено','Cancelled':'Отменено','Completed':'Завершено','Active':'Активно','Inactive':'Неактивно','Open':'Открыто','Closed':'Закрыто','Pending':'Ожидает','Approved':'Одобрено','Rejected':'Отклонено','Yes':'Да','No':'Нет','From':'С','To':'По','Country':'Страна','City':'Город','Address':'Адрес','Phone':'Телефон','Group':'Группа','Territory':'Территория','Category':'Категория','Department':'Отдел','Employee':'Сотрудник','Project':'Проект','Task':'Задача','Priority':'Приоритет','Progress':'Прогресс','Start Date':'Дата начала','End Date':'Дата окончания','Details':'Подробности','Filter':'Фильтр','Clear filter':'Сбросить фильтр','Add':'Добавить','Delete':'Удалить','Print':'Печать','Download':'Скачать','Submit':'Отправить','Back':'Назад','Next':'Далее','New Customer':'Новый клиент','Save Customer':'Сохранить клиента','Manage your customer records':'Управление записями клиентов','Search customers, invoices, items, tasks…':'Поиск клиентов, счетов, товаров и задач…','Open navigation':'Открыть меню','Close navigation':'Закрыть меню'
    },
    es: {
      'Name':'Nombre','Type':'Tipo','VAT / TRN':'IVA / NIF','Mobile':'Móvil','Email':'Correo electrónico','Connections':'Conexiones','View':'Ver','Individual':'Individual','Customer':'Cliente','Supplier':'Proveedor','Date':'Fecha','Status':'Estado','Amount':'Importe','Total':'Total','Currency':'Moneda','Action':'Acción','Description':'Descripción','Account':'Cuenta','Balance':'Saldo','Debit':'Débito','Credit':'Crédito','Reference':'Referencia','Posting Date':'Fecha de registro','Due Date':'Fecha de vencimiento','Customer Name':'Nombre del cliente','Item':'Artículo','Quantity':'Cantidad','Qty':'Cant.','Rate':'Tarifa','Tax':'Impuesto','Grand Total':'Total general','Paid':'Pagado','Unpaid':'No pagado','Overdue':'Vencido','Draft':'Borrador','Submitted':'Enviado','Cancelled':'Cancelado','Completed':'Completado','Active':'Activo','Inactive':'Inactivo','Open':'Abierto','Closed':'Cerrado','Pending':'Pendiente','Approved':'Aprobado','Rejected':'Rechazado','Yes':'Sí','No':'No','From':'Desde','To':'Hasta','Country':'País','City':'Ciudad','Address':'Dirección','Phone':'Teléfono','Group':'Grupo','Territory':'Territorio','Category':'Categoría','Department':'Departamento','Employee':'Empleado','Project':'Proyecto','Task':'Tarea','Priority':'Prioridad','Progress':'Progreso','Start Date':'Fecha inicial','End Date':'Fecha final','Details':'Detalles','Filter':'Filtro','Clear filter':'Limpiar filtro','Add':'Añadir','Delete':'Eliminar','Print':'Imprimir','Download':'Descargar','Submit':'Enviar','Back':'Atrás','Next':'Siguiente','New Customer':'Nuevo cliente','Save Customer':'Guardar cliente','Manage your customer records':'Administre sus registros de clientes','Search customers, invoices, items, tasks…':'Buscar clientes, facturas, artículos y tareas…','Open navigation':'Abrir navegación','Close navigation':'Cerrar navegación'
    },
  };

  let current = 'en';
  let observer = null;
  const textSources = new WeakMap();
  const attributeSources = new WeakMap();
  const remote = { ar:{}, tr:{}, ru:{}, es:{} };
  const requested = { ar:new Set(), tr:new Set(), ru:new Set(), es:new Set() };
  const pending = new Set();
  let remoteTimer = null;

  function dictionary(code) {
    if (code === 'en') return {};
    const result = Object.assign({}, common[code], ui[code]);
    Object.keys(navEnglish).forEach(key => { result[navEnglish[key]] = nav[code][key]; });
    return result;
  }

  function queueRemote(source) {
    if (current === 'en' || !source || source.length > 300 || requested[current].has(source)) return;
    requested[current].add(source);
    pending.add(source);
    clearTimeout(remoteTimer);
    remoteTimer = setTimeout(loadRemoteTranslations, 80);
  }

  async function loadRemoteTranslations() {
    if (current === 'en' || !pending.size) return;
    const language = current;
    const messages = Array.from(pending).slice(0, 400);
    messages.forEach(message => pending.delete(message));
    try {
      const query = new URLSearchParams({language, messages:JSON.stringify(messages)});
      const response = await fetch('/api/method/next_ai.api.get_portal_translations?' + query.toString(), {
        headers:{'X-Requested-With':'XMLHttpRequest'}
      });
      const payload = await response.json();
      if (!response.ok || payload.exc) throw new Error('Translation request failed');
      Object.assign(remote[language], payload.message || {});
      if (current === language) translateTree(document.body);
    } catch (error) {
      console.warn('Portal translations could not be loaded:', error);
    }
    if (pending.size) remoteTimer = setTimeout(loadRemoteTranslations, 80);
  }

  function translated(source) {
    const value = dictionary(current)[source] || remote[current]?.[source];
    if (value) return value;
    queueRemote(source);
    return source;
  }

  function translateTextNode(node) {
    if (!node.nodeValue || !node.nodeValue.trim()) return;
    let source = textSources.get(node);
    if (!source) { source = node.nodeValue.trim(); textSources.set(node, source); }
    const value = translated(source);
    node.nodeValue = node.nodeValue.replace(node.nodeValue.trim(), value);
  }

  function translateAttributes(element) {
    if (!element.getAttribute) return;
    let sources = attributeSources.get(element);
    if (!sources) { sources = {}; attributeSources.set(element, sources); }
    ['placeholder', 'title', 'aria-label'].forEach(name => {
      if (!element.hasAttribute(name)) return;
      if (!sources[name]) sources[name] = element.getAttribute(name);
      element.setAttribute(name, translated(sources[name]));
    });
  }

  function translateTree(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) return translateTextNode(root);
    translateAttributes(root);
    Array.from(root.childNodes || []).forEach(translateTree);
  }

  function apply(code, persist) {
    current = languages[code] ? code : 'en';
    document.documentElement.lang = current;
    document.documentElement.dir = languages[current].dir;
    document.body && document.body.classList.toggle('portal-rtl', languages[current].dir === 'rtl');
    const selector = document.getElementById('portal-language');
    if (selector) selector.value = current;
    translateTree(document.body);
    if (persist !== false) localStorage.setItem('apex_portal_language', current);
    document.dispatchEvent(new CustomEvent('portal:language-changed', { detail: { language: current } }));
  }

  function init() {
    current = localStorage.getItem('apex_portal_language') || 'en';
    apply(current, false);
    observer = new MutationObserver(records => records.forEach(record =>
      Array.from(record.addedNodes || []).forEach(translateTree)
    ));
    observer.observe(document.body, { childList: true, subtree: true });
  }

  window.PortalI18n = { init, apply, languages, get current() { return current; } };
}());
