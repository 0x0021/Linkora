// ============ pages/departments.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ 部门架构功能（懒加载）============
let departmentTree = null;
let selectedDeptMember = null;
const _expandedDeptIds = new Set();   // 记录用户已展开的部门

async function loadDepartments() {
    const container = document.getElementById('dept-tree');
    if (!container) return;
    
    if (departmentTree && container.dataset.loaded === '1') {
        return;
    }
    
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("⏳")}</div><p>加载中...</p></div>`;
    
    try {
        const data = await api.fetch('/api/departments/tree');
        
        if (data && data.success) {
            departmentTree = data.tree;
            departmentTree.forEach(n => n._loaded = false);
            renderDepartmentTree(departmentTree, container);
            container.dataset.loaded = '1';
        } else if (data && data.status === 401) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("🔒")}</div><p>请先登录</p></div>`;
        } else if (data && data.code === 'permission_denied') {
            container.innerHTML = `<div class="empty-state">
                    <div class="empty-icon">${iconize("🔒")}</div>
                    <p>${escapeHtml(data.error || '无权限访问')}</p>
                    <p class="empty-hint">请联系组织管理员开启 CLI 数据访问权限</p>
                </div>`;
        } else {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("❌")}</div><p>加载失败：${escapeHtml(data?.error || '')}</p></div>`;
        }
    } catch (error) {
        console.error('[部门架构] 加载失败：', error);
        container.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("❌")}</div><p>网络错误</p></div>`;
    }
}

function _createDeptNodeEl(node) {
    const nodeEl = document.createElement('div');
    nodeEl.className = 'dept-node';
    nodeEl.dataset.deptId = node.id;
    
    const header = document.createElement('div');
    header.className = 'dept-node-header';
    const hasToggle = node._loaded ? (node.children?.length || node.members?.length) : node.has_children;
    header.innerHTML = `
        <div class="dept-toggle ${hasToggle ? '' : 'leaf'}">${hasToggle ? iconize("▶") : ''}</div>
        <div class="dept-icon">${iconize("🏢")}</div>
        <div class="dept-name">${escapeHtml(node.name)}</div>
        <div class="dept-count">${node._loaded ? (node.members?.length || 0) : (node.member_count || '')}</div>
    `;
    
    header.addEventListener('click', () => {
        toggleDeptNode(nodeEl, node);
    });
    
    nodeEl.appendChild(header);
    return nodeEl;
}

function _createMemberEl(member) {
    const memberEl = document.createElement('div');
    memberEl.className = 'dept-member';
    const avatar = member.avatar || (member.name ? member.name[0] : '?');
    const initial = member.name ? escapeHtml(member.name[0]) : '?';
    memberEl.innerHTML = `
        <div class="dept-member-avatar">${member.avatar ? `<img src="${escapeHtml(avatar)}" alt="">` : initial}</div>
        <div class="dept-member-name">${escapeHtml(member.name || '未知')}</div>
        <div class="dept-member-title">${escapeHtml(member.title || '')}</div>
    `;
    memberEl.addEventListener('click', (e) => {
        e.stopPropagation();
        selectDeptMember(member);
    });
    return memberEl;
}

function _buildChildrenEl(node) {
    const wrap = document.createElement('div');
    wrap.className = 'dept-children';
    
    if (node.children && node.children.length) {
        node.children.forEach(child => {
            if (!('_loaded' in child)) child._loaded = false;
            wrap.appendChild(_createDeptNodeEl(child));
        });
    }
    if (node.members && node.members.length) {
        node.members.forEach(m => wrap.appendChild(_createMemberEl(m)));
    }
    return wrap;
}

function renderDepartmentTree(nodes, container) {
    container.innerHTML = '';
    nodes.forEach(node => {
        if (!('_loaded' in node)) node._loaded = false;
        container.appendChild(_createDeptNodeEl(node));
    });
}

async function toggleDeptNode(nodeEl, node) {
    const toggle = nodeEl.querySelector('.dept-toggle');
    
    // 已有子元素，仅折叠/展开
    let childrenEl = nodeEl.querySelector(':scope > .dept-children');
    if (childrenEl) {
        const expanded = childrenEl.classList.contains('expanded');
        if (expanded) {
            childrenEl.classList.remove('expanded');
            toggle.classList.remove('expanded');
            toggle.innerHTML = iconize("▶");
            _expandedDeptIds.delete(node.id);
        } else {
            childrenEl.classList.add('expanded');
            toggle.classList.add('expanded');
            toggle.innerHTML = iconize("▼");
            _expandedDeptIds.add(node.id);
        }
        return;
    }
    
    // 未加载过，异步拉取子部门和成员
    if (toggle.innerHTML !== iconize("⏳")) {
        toggle.innerHTML = iconize("⏳");
    }
    try {
        const [childrenResp, membersResp] = await Promise.all([
            api.fetch(`/api/departments/${node.id}/children`),
            api.fetch(`/api/departments/${node.id}/members`),
        ]);
        node.children = childrenResp.success ? (childrenResp.children || []) : [];
        node.members = membersResp.success ? (membersResp.members || []) : [];
        node._loaded = true;
        
        childrenEl = _buildChildrenEl(node);
        nodeEl.appendChild(childrenEl);
        
        // 更新头部计数为成员数
        const countEl = nodeEl.querySelector('.dept-count');
        if (countEl) countEl.textContent = node.members.length || '';
        
        if (node.children.length === 0 && node.members.length === 0) {
            // 叶子部门，隐藏 toggle
            toggle.classList.add('leaf');
            toggle.textContent = '';
            return;
        }
        
        childrenEl.classList.add('expanded');
        toggle.classList.add('expanded');
        toggle.innerHTML = iconize("▼");
        _expandedDeptIds.add(node.id);
        
    } catch (e) {
        console.error(`[部门架构] 展开 ${node.name} 失败:`, e);
        toggle.innerHTML = iconize("▶");
        nodeEl.querySelector('.empty-state')?.remove();
        const err = document.createElement('div');
        err.className = 'dept-error';
        err.textContent = '加载失败';
        nodeEl.appendChild(err);
    }
}

function selectDeptMember(member) {
    selectedDeptMember = member;
    document.getElementById('msg-search').value = member.name;
    loadMessages();
}

// 搜索部门/成员（本地过滤，仅扫描已加载数据）
document.getElementById('dept-search')?.addEventListener('input', (e) => {
    const query = e.target.value.trim().toLowerCase();
    const container = document.getElementById('dept-tree');
    if (!container || !departmentTree) return;
    if (!query) {
        renderDepartmentTree(departmentTree, container);
        return;
    }
    const filtered = departmentTree.map(n => filterNode(n, query)).filter(Boolean);
    if (filtered.length === 0) {
        container.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("🔍")}</div><p>未找到匹配项</p></div>`;
    } else {
        renderDepartmentTree(filtered, container);
    }
});

function filterNode(node, query) {
    if (node.name.toLowerCase().includes(query)) return node;
    let matchedMembers = [];
    if (node.members) {
        matchedMembers = node.members.filter(m => (m.name || '').toLowerCase().includes(query));
    }
    let matchedChildren = [];
    if (node.children) {
        matchedChildren = node.children.map(c => filterNode(c, query)).filter(Boolean);
    }
    if (matchedMembers.length > 0 || matchedChildren.length > 0) {
        return { ...node, members: matchedMembers, children: matchedChildren };
    }
    return null;
}

